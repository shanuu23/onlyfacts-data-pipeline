"""
Downloads AEMO NEM price-and-demand CSVs for all regions from January 2019 to present.
Files are saved directly (no zip) into Data/Operational/.
"""

import os
from datetime import date
from dateutil.relativedelta import relativedelta

import requests
from tqdm import tqdm

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "Data", "Operational")
BASE_URL   = "https://www.aemo.com.au/aemo/data/nem/priceanddemand/PRICE_AND_DEMAND_{year}{month:02d}_{region}.csv"
REGIONS    = ["NSW1", "QLD1", "SA1", "TAS1", "VIC1"]
START      = date(2019, 1, 1)


def months_to_download():
    current = START
    today   = date.today()
    # Include the current month — unlike NEMWEB rooftop archives, AEMO's
    # price-and-demand file is a rolling file updated daily throughout the month.
    while current <= today:
        yield current.year, current.month
        current += relativedelta(months=1)


def download_file(year: int, month: int, region: str, session: requests.Session) -> bool:
    filename = f"PRICE_AND_DEMAND_{year}{month:02d}_{region}.csv"
    out_path = os.path.join(OUTPUT_DIR, filename)

    if os.path.exists(out_path):
        return True

    url = BASE_URL.format(year=year, month=month, region=region)
    try:
        resp = session.get(url, timeout=60)
        resp.raise_for_status()
    except requests.HTTPError as e:
        print(f"  SKIP {filename}: HTTP {e.response.status_code}")
        return False
    except requests.RequestException as e:
        print(f"  ERROR {filename}: {e}")
        return False

    with open(out_path, "wb") as f:
        f.write(resp.content)
    return True


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    months = list(months_to_download())
    total  = len(months) * len(REGIONS)
    print(f"Downloading {len(months)} months × {len(REGIONS)} regions = {total} files → {OUTPUT_DIR}\n")

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (aemo-downloader)"

    ok = fail = 0
    for year, month in tqdm(months, unit="month"):
        for region in REGIONS:
            if download_file(year, month, region, session):
                ok += 1
            else:
                fail += 1

    print(f"\nDone. {ok} files saved/skipped, {fail} failed.")


if __name__ == "__main__":
    main()
