"""
Daily pipeline: fetches yesterday's rooftop and operational data from NEMWEB CURRENT,
transforms to daily GWh, and UPSERTs a single row into Postgres.

Run by GitHub Actions every day at 17:00 UTC (3 AM AEST / 4 AM AEDT).
Credentials are read from environment variables — never hardcoded.
"""

import os
import io
import zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import pandas as pd
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, text, URL

# ── Config ────────────────────────────────────────────────────────────────────

REGIONS     = ["NSW1", "QLD1", "SA1", "TAS1", "VIC1"]
ROOFTOP_URL = "https://nemweb.com.au/Reports/CURRENT/ROOFTOP_PV/ACTUAL/"
OP_URL      = "https://nemweb.com.au/Reports/CURRENT/Operational_Demand/ACTUAL_HH/"
EXPECTED_INTERVAL_FILES = 48

# ── Date ──────────────────────────────────────────────────────────────────────

def yesterday_aest() -> tuple[str, str]:
    """Return yesterday's date in Australian Eastern time as (YYYYMMDD, YYYY-MM-DD)."""
    tz   = ZoneInfo("Australia/Sydney")
    date = (datetime.now(tz) - timedelta(days=1)).date()
    return date.strftime("%Y%m%d"), str(date)

# ── Download helpers ──────────────────────────────────────────────────────────

def list_files(
    base_url: str,
    date_str: str,
    session: requests.Session,
    required_text: str | None = None,
) -> list:
    """Scrape a NEMWEB CURRENT directory and return all zip URLs matching date_str."""
    resp = session.get(base_url, timeout=30)
    resp.raise_for_status()
    soup  = BeautifulSoup(resp.text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if (
            date_str in href
            and href.endswith(".zip")
            and (required_text is None or required_text in href)
        ):
            # hrefs are relative paths — prepend host
            full = "https://nemweb.com.au" + href if href.startswith("/") else base_url + href
            links.append(full)
    # Directory listings sometimes duplicate entries
    return sorted(set(links))


def require_complete_day(label: str, urls: list, date_str: str) -> None:
    if len(urls) != EXPECTED_INTERVAL_FILES:
        raise RuntimeError(
            f"{label}: expected {EXPECTED_INTERVAL_FILES} interval files for "
            f"{date_str}, found {len(urls)}. Refusing to write a partial day."
        )


def fetch_csv_lines(url: str, session: requests.Session) -> list:
    """Download a zip and return the lines of the CSV inside."""
    resp = session.get(url, timeout=60)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        name = next(n for n in zf.namelist() if n.upper().endswith(".CSV"))
        return zf.read(name).decode("utf-8").splitlines()

# ── NEMWEB parser ─────────────────────────────────────────────────────────────

def parse_nemweb(lines: list) -> pd.DataFrame:
    """Parse NEMWEB C/I/D row format into a DataFrame.

    The I row defines column names but includes 4 metadata prefix tokens
    (e.g. I,ROOFTOP,ACTUAL,2) that are not part of the actual data schema.
    """
    header = next((l for l in lines if l.startswith("I,")), None)
    if not header:
        return pd.DataFrame()
    cols = [c.strip().strip('"') for c in header.split(",")][4:]
    rows = []
    for l in lines:
        if l.startswith("D,"):
            parts = [c.strip().strip('"') for c in l.split(",")]
            rows.append(parts[4 : 4 + len(cols)])
    return pd.DataFrame(rows, columns=cols)

# ── Rooftop ───────────────────────────────────────────────────────────────────

def fetch_rooftop(date_str: str, session: requests.Session) -> dict:
    """Download all 48 rooftop interval files for date_str and return daily aggregates."""
    urls = list_files(ROOFTOP_URL, date_str, session, required_text="_MEASUREMENT_")
    print(f"  Rooftop: {len(urls)} interval files found")
    require_complete_day("Rooftop", urls, date_str)

    frames = []
    for url in urls:
        df = parse_nemweb(fetch_csv_lines(url, session))
        if df.empty:
            continue
        # MEASUREMENT is the primary metered data.
        # SATELLITE (interpolated) and DAILY (retrospective) are excluded.
        df = df[
            (df["TYPE"] == "MEASUREMENT") &
            (df["REGIONID"].isin(REGIONS))
        ][["INTERVAL_DATETIME", "REGIONID", "POWER"]]
        frames.append(df)

    if not frames:
        raise RuntimeError(f"Rooftop: no parseable data found for {date_str}")

    df = pd.concat(frames, ignore_index=True)
    df["INTERVAL_DATETIME"] = pd.to_datetime(df["INTERVAL_DATETIME"])
    df["POWER"]             = pd.to_numeric(df["POWER"], errors="coerce")

    # Live rooftop files are always 30-min intervals
    gwh = df.groupby("REGIONID")["POWER"].sum() * 0.5 / 1000

    # NEM-wide series for peak & trough
    nem = df.groupby("INTERVAL_DATETIME")["POWER"].sum()

    return {
        **{f"{r}_rooftop_GWh": float(gwh.get(r, 0)) for r in REGIONS},
        "rooftop_GWh":      float(gwh.sum()),
        "max_rooftop_MW":   float(nem.max()),
        "max_rooftop_time": nem.idxmax(),
        "min_rooftop_MW":   float(nem.min()),
        "min_rooftop_time": nem.idxmin(),
    }

# ── Operational ───────────────────────────────────────────────────────────────

def fetch_operational(date_str: str, session: requests.Session) -> dict:
    """Download all 48 operational demand interval files for date_str and return daily aggregates."""
    urls = list_files(OP_URL, date_str, session)
    print(f"  Operational: {len(urls)} interval files found")
    require_complete_day("Operational", urls, date_str)

    frames = []
    for url in urls:
        df = parse_nemweb(fetch_csv_lines(url, session))
        if df.empty:
            continue
        df = df[df["REGIONID"].isin(REGIONS)][
            ["INTERVAL_DATETIME", "REGIONID", "OPERATIONAL_DEMAND"]
        ]
        frames.append(df)

    if not frames:
        raise RuntimeError(f"Operational: no parseable data found for {date_str}")

    df = pd.concat(frames, ignore_index=True)
    df["INTERVAL_DATETIME"]  = pd.to_datetime(df["INTERVAL_DATETIME"])
    # OPERATIONAL_DEMAND is parsed from strings — coerce handles any stray empty values
    df["OPERATIONAL_DEMAND"] = pd.to_numeric(df["OPERATIONAL_DEMAND"], errors="coerce")

    # Live operational CURRENT files are always HH (half-hourly = 30-min intervals)
    gwh = df.groupby("REGIONID")["OPERATIONAL_DEMAND"].sum() * 0.5 / 1000

    nem = df.groupby("INTERVAL_DATETIME")["OPERATIONAL_DEMAND"].sum()

    return {
        **{f"{r}_operational_GWh": float(gwh.get(r, 0)) for r in REGIONS},
        "operational_demand_GWh": float(gwh.sum()),
        "max_operational_MW":     float(nem.max()),
        "max_operational_time":   nem.idxmax(),
        "min_operational_MW":     float(nem.min()),
        "min_operational_time":   nem.idxmin(),
    }

# ── Postgres ──────────────────────────────────────────────────────────────────

def get_engine():
    """Build SQLAlchemy engine from environment variables. SSL is required by hosted providers."""
    url = URL.create(
        drivername="postgresql+psycopg2",
        username=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        database=os.environ["DB_NAME"],
        query={"sslmode": "require"},
    )
    return create_engine(url)


def quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def db_value(value):
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return value


def upsert_row(engine, row: dict):
    """UPSERT a single row into electricity_demand.

    ON CONFLICT (settlement_date) DO UPDATE makes reruns safe —
    rerunning the same day updates rather than duplicates.
    Wrapped in engine.begin() so the write is atomic.
    """
    cols    = ", ".join(quote_ident(k) for k in row.keys())
    vals    = ", ".join(f":{k}" for k in row.keys())
    updates = ", ".join(
        f"{quote_ident(k)} = EXCLUDED.{quote_ident(k)}"
        for k in row.keys()
        if k != "settlement_date"
    )
    params = {k: db_value(v) for k, v in row.items()}
    sql = text(f"""
        INSERT INTO {quote_ident("electricity_demand")} ({cols})
        VALUES ({vals})
        ON CONFLICT ({quote_ident("settlement_date")})
        DO UPDATE SET {updates}
    """)
    with engine.begin() as conn:
        conn.execute(sql, params)

# ── Main ──────────────────────────────────────────────────────────────────────

def build_daily_row(date_str: str, date_fmt: str, session: requests.Session) -> dict:
    print("Fetching rooftop data...")
    rt = fetch_rooftop(date_str, session)

    print("\nFetching operational data...")
    op = fetch_operational(date_str, session)

    return {
        "settlement_date": date_fmt,
        **{f"{r}_operational_GWh": op[f"{r}_operational_GWh"] for r in REGIONS},
        **{f"{r}_rooftop_GWh":     rt[f"{r}_rooftop_GWh"]     for r in REGIONS},
        "operational_demand_GWh": op["operational_demand_GWh"],
        "rooftop_GWh":            rt["rooftop_GWh"],
        "underlying_demand_GWh":  op["operational_demand_GWh"] + rt["rooftop_GWh"],
        "max_operational_MW":     op["max_operational_MW"],
        "max_operational_time":   op["max_operational_time"],
        "min_operational_MW":     op["min_operational_MW"],
        "min_operational_time":   op["min_operational_time"],
        "max_rooftop_MW":         rt["max_rooftop_MW"],
        "max_rooftop_time":       rt["max_rooftop_time"],
        "min_rooftop_MW":         rt["min_rooftop_MW"],
        "min_rooftop_time":       rt["min_rooftop_time"],
    }


def main():
    date_str, date_fmt = yesterday_aest()
    print(f"Pipeline running for {date_fmt} (AEST)\n")

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (onlyfacts-pipeline)"

    row = build_daily_row(date_str, date_fmt, session)

    print(f"\nRow summary:")
    print(f"  operational_demand_GWh : {row['operational_demand_GWh']:.3f}")
    print(f"  rooftop_GWh            : {row['rooftop_GWh']:.3f}")
    print(f"  underlying_demand_GWh  : {row['underlying_demand_GWh']:.3f}")
    print(f"  max_operational_MW     : {row['max_operational_MW']:.2f} at {row['max_operational_time']}")
    print(f"  max_rooftop_MW         : {row['max_rooftop_MW']:.2f} at {row['max_rooftop_time']}")

    print("\nConnecting to Postgres and upserting row...")
    engine = get_engine()
    upsert_row(engine, row)
    print(f"Done. {date_fmt} upserted successfully.")


if __name__ == "__main__":
    main()
