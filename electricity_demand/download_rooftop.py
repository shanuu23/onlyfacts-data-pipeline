"""
Downloads NEMWEB historical rooftop PV actual data from January 2019 to the current month.
Files are unzipped and saved as CSVs in electricity_demand/data/Rooftop/.
"""

import os
import zipfile
import io
from datetime import date
from dateutil.relativedelta import relativedelta

import requests
from tqdm import tqdm

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "data", "Rooftop")

# NEMWEB changed the filename convention in Aug 2024.
# Pre-Aug 2024:  PUBLIC_DVD_ROOFTOP_PV_ACTUAL_{YEAR}{MONTH}010000.zip
# Post-Aug 2024: PUBLIC_ARCHIVE#ROOFTOP_PV_ACTUAL#FILE01#{YEAR}{MONTH}010000.zip
# The # character is stored literally as %23 in the server's filename, so the %
# itself must be encoded as %25 — giving the double-encoded %2523.
_BASE = "https://nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/{year}/MMSDM_{year}_{month:02d}/MMSDM_Historical_Data_SQLLoader/DATA/"
OLD_URL  = _BASE + "PUBLIC_DVD_ROOFTOP_PV_ACTUAL_{year}{month:02d}010000.zip"
NEW_URL  = _BASE + "PUBLIC_ARCHIVE%2523ROOFTOP_PV_ACTUAL%2523FILE01%2523{year}{month:02d}010000.zip"
CUTOVER  = date(2024, 8, 1)

START = date(2019, 1, 1)


def url_for(year: int, month: int) -> str:
    if date(year, month, 1) < CUTOVER:
        return OLD_URL.format(year=year, month=month)
    return NEW_URL.format(year=year, month=month)


def months_to_download():
    current = START
    today   = date.today()
    # Stop at the first of the current month — NEMWEB only publishes complete months
    # so the current month's archive won't exist yet.
    end = today.replace(day=1)
    while current < end:
        yield current.year, current.month
        current += relativedelta(months=1)


def download_month(year: int, month: int, session: requests.Session) -> bool:
    url = url_for(year, month)
    try:
        resp = session.get(url, timeout=60, stream=True)
        resp.raise_for_status()
    except requests.HTTPError as e:
        print(f"  SKIP {year}-{month:02d}: HTTP {e.response.status_code}")
        return False
    except requests.RequestException as e:
        print(f"  ERROR {year}-{month:02d}: {e}")
        return False

    zip_bytes = io.BytesIO(resp.content)
    try:
        with zipfile.ZipFile(zip_bytes) as zf:
            for name in zf.namelist():
                if name.upper().endswith(".CSV"):
                    out_path = os.path.join(OUTPUT_DIR, name)
                    if not os.path.exists(out_path):
                        with open(out_path, "wb") as f:
                            f.write(zf.read(name))
                    else:
                        print(f"  EXISTS {name}, skipping")
    except zipfile.BadZipFile:
        print(f"  BAD ZIP {year}-{month:02d}")
        return False

    return True


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    months = list(months_to_download())
    print(f"Downloading {len(months)} months of rooftop PV data → {OUTPUT_DIR}\n")

    session = requests.Session()
    # Some servers reject requests without a browser-like User-Agent
    session.headers["User-Agent"] = "Mozilla/5.0 (nemweb-downloader)"

    ok = fail = 0
    for year, month in tqdm(months, unit="month"):
        result = download_month(year, month, session)
        if result:
            ok += 1
        else:
            fail += 1

    print(f"\nDone. {ok} downloaded, {fail} failed/skipped.")


if __name__ == "__main__":
    main()
