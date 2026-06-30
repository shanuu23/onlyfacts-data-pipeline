"""
Builds combined_dataset.csv from raw operational and rooftop data.
Output: Data/combined_dataset.csv — daily resolution, Jan 2019 to present.

Column order:
  settlement_date
  NSW1_operational_GWh ... VIC1_operational_GWh   (per-state daily energy)
  NSW1_rooftop_GWh     ... VIC1_rooftop_GWh        (per-state daily energy)
  operational_demand_GWh, rooftop_GWh, underlying_demand_GWh  (NEM-wide totals)
  max/min operational MW + time, max/min rooftop MW + time     (NEM-wide peaks)
"""

import os
import glob
import pandas as pd
from tqdm import tqdm

OPERATIONAL_DIR = os.path.join("Data", "Operational")
ROOFTOP_DIR     = os.path.join("Data", "Rooftop")
OUTPUT_FILE     = os.path.join("Data", "combined_dataset.csv")

REGIONS = ["NSW1", "QLD1", "SA1", "TAS1", "VIC1"]


def interval_hours(df: pd.DataFrame, time_col: str) -> float:
    """Infer interval length in hours from the two earliest timestamps.

    The NEM switched from 30-min to 5-min settlement on Oct 1, 2021.
    Pre-Oct 2021 files have 48 intervals/day (0.5h each).
    Post-Oct 2021 files have 288 intervals/day (1/12h each).
    Applying the wrong factor inflates or deflates daily GWh by 6x.
    """
    ts = df[time_col].sort_values().iloc[:2]
    return (ts.iloc[1] - ts.iloc[0]).seconds / 3600


# ── Operational ───────────────────────────────────────────────────────────────

def load_operational() -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(OPERATIONAL_DIR, "*.csv")))
    frames = []
    for f in tqdm(files, desc="Operational", unit="file"):
        df = pd.read_csv(f, parse_dates=["SETTLEMENTDATE"])
        # TRADE is the standard half-hourly settlement period.
        # Other values (e.g. INVALID) indicate revised or rejected intervals
        # that should not be counted toward daily energy.
        df = df[(df["PERIODTYPE"] == "TRADE") & (df["REGION"].isin(REGIONS))]
        ih = interval_hours(df, "SETTLEMENTDATE")
        df = df[["SETTLEMENTDATE", "REGION", "TOTALDEMAND"]].copy()
        df["_gwh"] = df["TOTALDEMAND"] * ih / 1000
        frames.append(df)

    df = pd.concat(frames, ignore_index=True)
    df["date"] = df["SETTLEMENTDATE"].dt.date

    region_daily = (
        df.groupby(["date", "REGION"])["_gwh"]
        .sum()
    ).unstack("REGION")[REGIONS]
    region_daily.columns = [f"{r}_operational_GWh" for r in REGIONS]

    # Sum all states per interval to get NEM-wide MW, then find daily peak & trough
    nem = df.groupby("SETTLEMENTDATE")["TOTALDEMAND"].sum().reset_index()
    nem["date"] = nem["SETTLEMENTDATE"].dt.date

    max_rows = nem.loc[nem.groupby("date")["TOTALDEMAND"].idxmax()].set_index("date")
    min_rows = nem.loc[nem.groupby("date")["TOTALDEMAND"].idxmin()].set_index("date")

    peaks = pd.DataFrame({
        "max_operational_MW":   max_rows["TOTALDEMAND"],
        "max_operational_time": max_rows["SETTLEMENTDATE"],
        "min_operational_MW":   min_rows["TOTALDEMAND"],
        "min_operational_time": min_rows["SETTLEMENTDATE"],
    })

    return region_daily.join(peaks)


# ── Rooftop ───────────────────────────────────────────────────────────────────

def _parse_rooftop_csv(path: str) -> pd.DataFrame:
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    # NEMWEB files use a custom format: C = comment, I = header, D = data.
    # The I row defines column names but prefixes them with 4 metadata tokens
    # (e.g. I,ROOFTOP,ACTUAL,2,...) that are not part of the actual data schema.
    header = next((l for l in lines if l.startswith("I,")), None)
    if not header:
        return pd.DataFrame()

    all_cols  = [c.strip().strip('"') for c in header.strip().split(",")]
    data_cols = all_cols[4:]  # skip the I/ROOFTOP/ACTUAL/2 prefix tokens

    rows = []
    for l in lines:
        if l.startswith("D,"):
            parts = [c.strip().strip('"') for c in l.strip().split(",")]
            rows.append(parts[4 : 4 + len(data_cols)])

    return pd.DataFrame(rows, columns=data_cols)


def load_rooftop() -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(ROOFTOP_DIR, "*.CSV")))
    frames = []
    for f in tqdm(files, desc="Rooftop   ", unit="file"):
        df = _parse_rooftop_csv(f)
        if df.empty:
            continue
        # MEASUREMENT = primary metered data (what we want)
        # SATELLITE   = interpolated estimate, lower quality
        # DAILY       = retrospective revision, only present in early 2019 files
        df = df[
            (df["TYPE"] == "MEASUREMENT") &
            (df["REGIONID"].isin(REGIONS))
        ][["INTERVAL_DATETIME", "REGIONID", "POWER"]]
        frames.append(df)

    df = pd.concat(frames, ignore_index=True)
    df["INTERVAL_DATETIME"] = pd.to_datetime(df["INTERVAL_DATETIME"])
    # POWER comes from string parsing so coerce handles any stray empty strings
    df["POWER"] = pd.to_numeric(df["POWER"], errors="coerce")
    df["date"]  = df["INTERVAL_DATETIME"].dt.date

    # Rooftop data has always been published at 30-min resolution — no dynamic
    # detection needed unlike operational which switched to 5-min in Oct 2021.
    region_daily = (
        df.groupby(["date", "REGIONID"])["POWER"]
        .sum() * (0.5 / 1000)
    ).unstack("REGIONID")[REGIONS]
    region_daily.columns = [f"{r}_rooftop_GWh" for r in REGIONS]

    nem = df.groupby("INTERVAL_DATETIME")["POWER"].sum().reset_index()
    nem["date"] = nem["INTERVAL_DATETIME"].dt.date

    max_rows = nem.loc[nem.groupby("date")["POWER"].idxmax()].set_index("date")
    min_rows = nem.loc[nem.groupby("date")["POWER"].idxmin()].set_index("date")

    peaks = pd.DataFrame({
        "max_rooftop_MW":   max_rows["POWER"],
        "max_rooftop_time": max_rows["INTERVAL_DATETIME"],
        "min_rooftop_MW":   min_rows["POWER"],
        "min_rooftop_time": min_rows["INTERVAL_DATETIME"],
    })

    return region_daily.join(peaks)


# ── Combine ───────────────────────────────────────────────────────────────────

def main():
    print("Loading operational data...")
    op = load_operational()
    print(f"  → {len(op):,} days\n")

    print("Loading rooftop data...")
    rt = load_rooftop()
    print(f"  → {len(rt):,} days\n")

    print("Joining and computing totals...")
    # Outer join preserves days where one source has data and the other doesn't
    # (e.g. current month has operational but rooftop archive not published yet).
    df = op.join(rt, how="outer").sort_index()
    df.index.name = "settlement_date"

    op_cols = [f"{r}_operational_GWh" for r in REGIONS]
    rt_cols = [f"{r}_rooftop_GWh"     for r in REGIONS]

    df["operational_demand_GWh"] = df[op_cols].sum(axis=1)
    df["rooftop_GWh"]            = df[rt_cols].sum(axis=1)
    df["underlying_demand_GWh"]  = df["operational_demand_GWh"] + df["rooftop_GWh"]

    final_cols = (
        op_cols + rt_cols
        + ["operational_demand_GWh", "rooftop_GWh", "underlying_demand_GWh"]
        + ["max_operational_MW", "max_operational_time",
           "min_operational_MW", "min_operational_time"]
        + ["max_rooftop_MW",     "max_rooftop_time",
           "min_rooftop_MW",     "min_rooftop_time"]
    )
    df = df[final_cols]

    os.makedirs("Data", exist_ok=True)
    df.to_csv(OUTPUT_FILE)

    print(f"\nSaved:  {OUTPUT_FILE}")
    print(f"Shape:  {len(df):,} rows × {len(df.columns)} columns")
    print(f"\nColumns:\n  " + "\n  ".join(df.columns.tolist()))
    print(f"\nSample:\n{df.head(3).to_string()}")


if __name__ == "__main__":
    main()
