"""
Backfill missing daily rows into Supabase/Postgres.

Usage:
    python electricity_demand/backfill_daily.py 2026-06-17 2026-06-29
    python electricity_demand/backfill_daily.py 2026-06-17 2026-06-29 --dry-run

The end date is inclusive. Dates must be complete historical days in
Australia/Sydney time; the script refuses to run for today or future dates.
"""

import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from daily_pipeline import build_daily_row, get_engine, upsert_row


def parse_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a valid date. Use YYYY-MM-DD."
        ) from exc


def iter_dates(start, end):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def latest_complete_date():
    tz = ZoneInfo("Australia/Sydney")
    return (datetime.now(tz) - timedelta(days=1)).date()


def make_session():
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (onlyfacts-backfill)"
    return session


def print_summary(row: dict) -> None:
    print("Row summary:")
    print(f"  operational_demand_GWh : {row['operational_demand_GWh']:.3f}")
    print(f"  rooftop_GWh            : {row['rooftop_GWh']:.3f}")
    print(f"  underlying_demand_GWh  : {row['underlying_demand_GWh']:.3f}")


def main():
    parser = argparse.ArgumentParser(
        description="Backfill missing electricity_demand rows for an inclusive date range."
    )
    parser.add_argument("start_date", type=parse_date, help="Start date, YYYY-MM-DD")
    parser.add_argument("end_date", type=parse_date, help="End date, YYYY-MM-DD")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and aggregate rows, but do not write to Postgres.",
    )
    args = parser.parse_args()

    if args.start_date > args.end_date:
        parser.error("start_date must be before or equal to end_date")

    max_date = latest_complete_date()
    if args.end_date > max_date:
        parser.error(
            f"end_date must be {max_date} or earlier because today is incomplete "
            "in Australia/Sydney time"
        )

    session = make_session()
    engine = None if args.dry_run else get_engine()

    total = 0
    for day in iter_dates(args.start_date, args.end_date):
        date_fmt = day.isoformat()
        date_str = day.strftime("%Y%m%d")
        print(f"\nBackfilling {date_fmt}...")

        row = build_daily_row(date_str, date_fmt, session)
        print_summary(row)

        if args.dry_run:
            print("Dry run: not writing row.")
        else:
            upsert_row(engine, row)
            print(f"Done. {date_fmt} upserted successfully.")

        total += 1

    mode = "checked" if args.dry_run else "upserted"
    print(f"\nBackfill complete. {total} day(s) {mode}.")


if __name__ == "__main__":
    main()
