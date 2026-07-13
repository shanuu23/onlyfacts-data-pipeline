"""Download and transform NTESMO weekly generation-mix data into daily GWh.

Postgres upsert behavior will be added only after the source download succeeds
from GitHub Actions.

Example:
    python ntesmo/daily_pipeline.py --file /path/to/weekly.csv --dry-run
    python ntesmo/daily_pipeline.py --dry-run
"""

from __future__ import annotations

import argparse
import io
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup


INTERVAL_HOURS = 0.5
EXPECTED_INTERVALS = 48
LISTING_URL = (
    "https://ntesmo.com.au/data/data-dashboard/downloads-asset-listing"
    "?result_371821_result_page=1&root=371900"
)
REQUEST_TIMEOUT = 60
FILE_PATTERN = re.compile(
    r"^Generation_mix_data_"
    r"(?P<start>\d{2}_[A-Za-z]+_\d{4})_-_"
    r"(?P<end>\d{2}_[A-Za-z]+_\d{4})\.csv$",
    re.IGNORECASE,
)

SOURCE_COLUMNS = [
    "timestamp",
    "DK_Fossil fuel",
    "DK_Utility Solar",
    "DK_Biomass",
    "DK_Steam",
    "DK_Distributed_PV estimated",
    "AS_Fossil fuel",
    "AS_Utility Solar",
    "AS_Distributed PV estimated",
    "TC_Fossil fuel",
    "TC_Distributed PV estimated",
]

SOURCE_TO_OUTPUT = {
    "DK_Fossil fuel": "DK_fossil_fuel_GWh",
    "DK_Utility Solar": "DK_utility_solar_GWh",
    "DK_Biomass": "DK_biomass_GWh",
    "DK_Steam": "DK_steam_GWh",
    "DK_Distributed_PV estimated": "DK_distributed_PV_estimated_GWh",
    "AS_Fossil fuel": "AS_fossil_fuel_GWh",
    "AS_Utility Solar": "AS_utility_solar_GWh",
    "AS_Distributed PV estimated": "AS_distributed_PV_estimated_GWh",
    "TC_Fossil fuel": "TC_fossil_fuel_GWh",
    "TC_Distributed PV estimated": "TC_distributed_PV_estimated_GWh",
}

DISTRIBUTED_PV_COLUMNS = [
    "DK_Distributed_PV estimated",
    "AS_Distributed PV estimated",
    "TC_Distributed PV estimated",
]

OUTPUT_COLUMNS = [
    "settlement_date",
    "DK_fossil_fuel_GWh",
    "DK_utility_solar_GWh",
    "DK_biomass_GWh",
    "DK_steam_GWh",
    "DK_distributed_PV_estimated_GWh",
    "AS_fossil_fuel_GWh",
    "AS_utility_solar_GWh",
    "AS_distributed_PV_estimated_GWh",
    "TC_fossil_fuel_GWh",
    "TC_distributed_PV_estimated_GWh",
    "NT_fossil_fuel_GWh",
    "NT_utility_solar_GWh",
    "NT_biomass_GWh",
    "NT_steam_GWh",
    "NT_distributed_PV_estimated_GWh",
]


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept-Language": "en-AU,en;q=0.9",
        }
    )
    return session


def is_cloudflare_challenge(response: requests.Response) -> bool:
    if response.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    content_type = response.headers.get("Content-Type", "").lower()
    preview = response.content[:4096].lower()
    return (
        "text/html" in content_type
        and (b"cloudflare" in preview or b"just a moment" in preview)
    )


def require_success(response: requests.Response, label: str) -> None:
    if is_cloudflare_challenge(response):
        raise RuntimeError(
            f"{label} was blocked by a Cloudflare browser challenge "
            f"(HTTP {response.status_code}). No cookies or bypass were attempted."
        )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"{label} returned HTTP {response.status_code}: {response.url}"
        ) from exc


def filename_from_url(url: str) -> str:
    return unquote(Path(urlparse(url).path).name)


def parse_filename_dates(filename: str):
    match = FILE_PATTERN.match(filename)
    if not match:
        return None
    try:
        start = datetime.strptime(match.group("start"), "%d_%B_%Y").date()
        end = datetime.strptime(match.group("end"), "%d_%B_%Y").date()
    except ValueError:
        return None
    return start, end


def select_latest_download(html: str) -> tuple[str, str]:
    """Select the generation-mix link with the latest filename end date."""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for link in soup.find_all("a", href=True):
        url = urljoin(LISTING_URL, link["href"])
        filename = filename_from_url(url)
        dates = parse_filename_dates(filename)
        if dates:
            candidates.append((dates[1], url, filename))

    if not candidates:
        raise RuntimeError(
            "No NTESMO generation-mix CSV links were found on the first listing page"
        )

    _, url, filename = max(candidates, key=lambda candidate: candidate[0])
    print(f"Latest source file: {filename}")
    print(f"Download URL: {url}")
    return url, filename


def discover_latest_download(session: requests.Session) -> tuple[str, str]:
    """Return the newest generation-mix asset URL and filename from page one."""
    try:
        response = session.get(LISTING_URL, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not request NTESMO listing page: {exc}") from exc
    require_success(response, "NTESMO listing page")
    return select_latest_download(response.text)


def download_weekly_source(
    session: requests.Session,
    url: str,
) -> tuple[io.BytesIO, str]:
    filename = filename_from_url(url)
    if not parse_filename_dates(filename):
        raise RuntimeError(f"Unexpected NTESMO source filename: {filename}")

    try:
        response = session.get(
            url,
            headers={"Referer": LISTING_URL},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not download NTESMO source file: {exc}") from exc
    require_success(response, f"NTESMO source file {filename}")

    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" in content_type or response.content.lstrip().startswith(b"<"):
        raise RuntimeError(
            f"NTESMO source file {filename} returned HTML instead of tabular data"
        )
    if not response.content:
        raise RuntimeError(f"NTESMO source file {filename} was empty")

    return io.BytesIO(response.content), filename


def read_weekly_source(source, source_name: str) -> pd.DataFrame:
    """Read a comma- or tab-delimited NTESMO source and validate its schema."""
    try:
        # NTESMO downloads are comma-delimited, while copied/exported samples
        # may be tab-delimited. The Python parser safely detects either form.
        df = pd.read_csv(source, sep=None, engine="python")
    except (OSError, pd.errors.ParserError) as exc:
        raise RuntimeError(
            f"Could not read NTESMO source file {source_name}: {exc}"
        ) from exc

    df.columns = [str(column).strip() for column in df.columns]
    missing = [column for column in SOURCE_COLUMNS if column not in df.columns]
    unexpected = [column for column in df.columns if column not in SOURCE_COLUMNS]
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"missing columns: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected columns: {', '.join(unexpected)}")
        raise RuntimeError("Invalid NTESMO source schema; " + "; ".join(details))

    return df[SOURCE_COLUMNS].copy()


def parse_and_validate_values(df: pd.DataFrame) -> pd.DataFrame:
    """Parse timestamps and numbers, allowing blank distributed-PV cells only."""
    result = df.copy()
    timestamp_text = result["timestamp"].astype(str).str.strip()
    timestamps = pd.to_datetime(
        timestamp_text,
        format="%d/%m/%Y %H:%M",
        errors="coerce",
    )
    two_digit_year = timestamps.isna()
    if two_digit_year.any():
        timestamps.loc[two_digit_year] = pd.to_datetime(
            timestamp_text.loc[two_digit_year],
            format="%d/%m/%y %H:%M",
            errors="coerce",
        )
    if timestamps.isna().any():
        invalid_values = timestamp_text.loc[timestamps.isna()].unique()
        raise RuntimeError(
            "Invalid NTESMO timestamp value(s): " + ", ".join(invalid_values)
        )
    result["timestamp"] = timestamps

    if result["timestamp"].duplicated().any():
        duplicates = result.loc[
            result["timestamp"].duplicated(keep=False), "timestamp"
        ].dt.strftime("%Y-%m-%d %H:%M")
        raise RuntimeError(
            "Duplicate NTESMO timestamps found: "
            + ", ".join(sorted(duplicates.unique()))
        )

    for column in SOURCE_COLUMNS[1:]:
        original_missing = result[column].isna() | result[column].astype(str).str.strip().eq("")
        numeric = pd.to_numeric(result[column], errors="coerce")
        invalid = numeric.isna() & ~original_missing
        if invalid.any():
            timestamps = result.loc[invalid, "timestamp"].dt.strftime("%Y-%m-%d %H:%M")
            raise RuntimeError(
                f"Invalid numeric value in {column} at: "
                + ", ".join(timestamps.tolist())
            )

        if original_missing.any() and column not in DISTRIBUTED_PV_COLUMNS:
            timestamps = result.loc[original_missing, "timestamp"].dt.strftime(
                "%Y-%m-%d %H:%M"
            )
            raise RuntimeError(
                f"Missing required value in {column} at: "
                + ", ".join(timestamps.tolist())
            )

        if original_missing.any():
            timestamps = result.loc[original_missing, "timestamp"].dt.strftime(
                "%Y-%m-%d %H:%M"
            )
            print(
                f"Warning: treating {len(timestamps)} blank {column} value(s) as zero "
                f"at {', '.join(timestamps.tolist())}"
            )

        result[column] = numeric.fillna(0.0)

    return result


def expected_timestamps(day: pd.Timestamp) -> pd.DatetimeIndex:
    return pd.date_range(day.normalize(), periods=EXPECTED_INTERVALS, freq="30min")


def validate_week(df: pd.DataFrame) -> None:
    """Require one Monday-Sunday week with complete days or the known Sunday cutoff."""
    dates = pd.DatetimeIndex(df["timestamp"].dt.normalize().unique()).sort_values()
    if len(dates) != 7:
        raise RuntimeError(f"Expected 7 dates in weekly file, found {len(dates)}")
    if dates[0].weekday() != 0 or dates[-1].weekday() != 6:
        raise RuntimeError(
            "Expected weekly file to span Monday through Sunday, found "
            f"{dates[0].date()} through {dates[-1].date()}"
        )
    if not dates.equals(pd.date_range(dates[0], periods=7, freq="D")):
        raise RuntimeError("Weekly source contains a gap between settlement dates")

    for day in dates:
        actual = pd.DatetimeIndex(
            df.loc[df["timestamp"].dt.normalize() == day, "timestamp"]
        ).sort_values()
        expected = expected_timestamps(day)
        missing = expected.difference(actual)
        unexpected = actual.difference(expected)

        if unexpected.empty and actual.equals(expected):
            print(f"  {day.date()}: 48 intervals")
            continue

        allowed_sunday_missing = pd.DatetimeIndex(
            [
                day.normalize() + pd.Timedelta(hours=23),
                day.normalize() + pd.Timedelta(hours=23, minutes=30),
            ]
        )
        if (
            day.weekday() == 6
            and len(actual) == 46
            and unexpected.empty
            and missing.equals(allowed_sunday_missing)
        ):
            print(f"  {day.date()}: 46 intervals (accepted Sunday 22:30 cutoff)")
            continue

        missing_text = ", ".join(timestamp.strftime("%H:%M") for timestamp in missing)
        unexpected_text = ", ".join(
            timestamp.strftime("%Y-%m-%d %H:%M") for timestamp in unexpected
        )
        raise RuntimeError(
            f"Invalid intervals for {day.date()}: found {len(actual)}; "
            f"missing [{missing_text}]; unexpected [{unexpected_text}]"
        )


def transform_to_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate half-hourly MW readings into the historical daily GWh schema."""
    result = df.copy()
    result["settlement_date"] = result["timestamp"].dt.date

    daily = (
        result.groupby("settlement_date", sort=True)[list(SOURCE_TO_OUTPUT)]
        .sum()
        .mul(INTERVAL_HOURS / 1000)
        .rename(columns=SOURCE_TO_OUTPUT)
        .reset_index()
    )

    daily["NT_fossil_fuel_GWh"] = (
        daily["DK_fossil_fuel_GWh"]
        + daily["AS_fossil_fuel_GWh"]
        + daily["TC_fossil_fuel_GWh"]
    )
    daily["NT_utility_solar_GWh"] = (
        daily["DK_utility_solar_GWh"] + daily["AS_utility_solar_GWh"]
    )
    daily["NT_biomass_GWh"] = daily["DK_biomass_GWh"]
    daily["NT_steam_GWh"] = daily["DK_steam_GWh"]
    daily["NT_distributed_PV_estimated_GWh"] = (
        daily["DK_distributed_PV_estimated_GWh"]
        + daily["AS_distributed_PV_estimated_GWh"]
        + daily["TC_distributed_PV_estimated_GWh"]
    )

    numeric_columns = [column for column in OUTPUT_COLUMNS if column != "settlement_date"]
    daily[numeric_columns] = daily[numeric_columns].round(10)
    return daily[OUTPUT_COLUMNS]


def validate_source_dates(daily: pd.DataFrame, source_name: str) -> None:
    filename_dates = parse_filename_dates(source_name)
    if not filename_dates:
        return
    expected_start, expected_end = filename_dates
    actual_start = daily["settlement_date"].min()
    actual_end = daily["settlement_date"].max()
    if (actual_start, actual_end) != (expected_start, expected_end):
        raise RuntimeError(
            f"Source filename declares {expected_start} through {expected_end}, "
            f"but its rows contain {actual_start} through {actual_end}"
        )


def transform_weekly_source(source, source_name: str) -> pd.DataFrame:
    source = read_weekly_source(source, source_name)
    parsed = parse_and_validate_values(source)
    print("Validating weekly intervals...")
    validate_week(parsed)
    daily = transform_to_daily(parsed)
    validate_source_dates(daily, source_name)
    return daily


def transform_weekly_file(path: Path) -> pd.DataFrame:
    return transform_weekly_source(path, path.name)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transform an NTESMO weekly generation-mix file into daily GWh."
    )
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--file",
        type=Path,
        help="Local weekly NTESMO CSV file; omit to discover the latest download.",
    )
    source_group.add_argument(
        "--url",
        help="Direct NTESMO weekly CSV URL; primarily useful for connectivity checks.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Transform and print rows without any database writes.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for writing the transformed daily CSV.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.file:
        print(f"Reading local source file: {args.file}")
        daily = transform_weekly_file(args.file)
    else:
        session = create_session()
        if args.url:
            url = args.url
            print(f"Using direct source URL: {url}")
        else:
            url, _ = discover_latest_download(session)
        source, source_name = download_weekly_source(session, url)
        daily = transform_weekly_source(source, source_name)

    print("\nDaily GWh rows:")
    print(daily.to_string(index=False))

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        daily.to_csv(args.output, index=False)
        print(f"\nSaved transformed rows to {args.output}")

    if args.dry_run:
        print("\nDry run complete. No database writes were performed.")
    else:
        print("\nTransformation complete. Database writes are not implemented yet.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        raise SystemExit(f"Error: {exc}") from exc
