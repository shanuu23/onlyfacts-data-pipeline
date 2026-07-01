"""
Patch combined_dataset.csv with aggregate rooftop solar totals from an XLSX file.

This is a one-time fallback for periods where the NEMWEB monthly rooftop archive
is not available yet, but an aggregate daily rooftop total is available.

Usage:
    python apply_rooftop_totals.py 2026-06-01 2026-06-30
    python apply_rooftop_totals.py 2026-06-01 2026-06-30 --dry-run

Only rooftop_GWh and underlying_demand_GWh are updated. State rooftop columns and
rooftop peak/trough fields are left unchanged because the XLSX source only has
NEM-wide daily totals.
"""

import argparse
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pandas as pd

DEFAULT_CSV = Path("Data/combined_dataset.csv")
DEFAULT_XLSX = Path("Data/2026_solar_data.xlsx")

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"main": MAIN_NS, "rel": REL_NS, "pkgrel": PKG_REL_NS}


def parse_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a valid date. Use YYYY-MM-DD."
        ) from exc


def excel_date(serial: float):
    return (datetime(1899, 12, 30) + timedelta(days=float(serial))).date()


def cell_col_index(cell_ref: str) -> int:
    col = "".join(ch for ch in cell_ref if ch.isalpha())
    idx = 0
    for ch in col:
        idx = idx * 26 + ord(ch.upper()) - ord("A") + 1
    return idx - 1


def read_shared_strings(zf: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []

    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    values = []
    for item in root.findall("main:si", NS):
        parts = [node.text or "" for node in item.findall(".//main:t", NS)]
        values.append("".join(parts))
    return values


def first_sheet_path(zf: ZipFile) -> str:
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rel_map = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels.findall("pkgrel:Relationship", NS)
    }

    sheet = workbook.find("main:sheets/main:sheet", NS)
    if sheet is None:
        raise ValueError("Workbook has no sheets")

    rel_id = sheet.attrib[f"{{{REL_NS}}}id"]
    target = rel_map[rel_id].lstrip("/")
    return "xl/" + target


def cell_value(cell, shared_strings: list[str]):
    value_node = cell.find("main:v", NS)
    if value_node is None:
        inline = cell.find("main:is/main:t", NS)
        return inline.text if inline is not None else None

    value = value_node.text
    if cell.attrib.get("t") == "s":
        return shared_strings[int(value)]
    return value


def read_xlsx_rows(path: Path) -> list[list[object]]:
    with ZipFile(path) as zf:
        shared_strings = read_shared_strings(zf)
        sheet_path = first_sheet_path(zf)
        root = ET.fromstring(zf.read(sheet_path))

        rows = []
        for row_el in root.findall(".//main:sheetData/main:row", NS):
            row = []
            for cell in row_el.findall("main:c", NS):
                idx = cell_col_index(cell.attrib.get("r", "A1"))
                while len(row) <= idx:
                    row.append(None)
                row[idx] = cell_value(cell, shared_strings)
            rows.append(row)

    max_cols = max((len(row) for row in rows), default=0)
    for row in rows:
        row.extend([None] * (max_cols - len(row)))
    return rows


def normalize_header(value) -> str:
    return str(value or "").strip().lower()


def load_rooftop_totals(path: Path) -> pd.DataFrame:
    rows = read_xlsx_rows(path)
    if not rows:
        raise ValueError(f"{path} is empty")

    headers = rows[0]
    normalized = [normalize_header(header) for header in headers]

    try:
        date_idx = normalized.index("date")
    except ValueError as exc:
        raise ValueError("Could not find a 'date' column in rooftop XLSX") from exc

    solar_idx = next(
        (
            idx
            for idx, header in enumerate(normalized)
            if "solar" in header and "rooftop" in header and "gwh" in header
        ),
        None,
    )
    if solar_idx is None:
        raise ValueError("Could not find a rooftop solar GWh column in rooftop XLSX")

    records = []
    for row in rows[1:]:
        raw_date = row[date_idx]
        raw_gwh = row[solar_idx]
        if raw_date in (None, "") or raw_gwh in (None, ""):
            continue

        try:
            day = excel_date(float(raw_date))
        except (TypeError, ValueError):
            day = pd.to_datetime(raw_date).date()

        records.append({
            "settlement_date": day,
            "rooftop_GWh_xlsx": float(raw_gwh),
        })

    return pd.DataFrame(records)


def patch_csv(csv_path: Path, totals: pd.DataFrame, start_date, end_date, dry_run: bool) -> None:
    df = pd.read_csv(csv_path, parse_dates=["settlement_date"])
    df["_date"] = df["settlement_date"].dt.date

    target_dates = set(pd.date_range(start_date, end_date).date)
    totals = totals[
        (totals["settlement_date"] >= start_date)
        & (totals["settlement_date"] <= end_date)
    ].copy()

    csv_dates = set(df["_date"])
    xlsx_dates = set(totals["settlement_date"])
    missing_in_csv = sorted(target_dates - csv_dates)
    missing_in_xlsx = sorted(target_dates - xlsx_dates)

    lookup = dict(zip(totals["settlement_date"], totals["rooftop_GWh_xlsx"]))
    target_mask = df["_date"].isin(target_dates & xlsx_dates)
    update_count = int(target_mask.sum())

    for idx in df.index[target_mask]:
        rooftop_gwh = lookup[df.at[idx, "_date"]]
        df.at[idx, "rooftop_GWh"] = rooftop_gwh
        df.at[idx, "underlying_demand_GWh"] = (
            df.at[idx, "operational_demand_GWh"] + rooftop_gwh
        )

    print(f"CSV: {csv_path}")
    print(f"XLSX dates: {totals['settlement_date'].min()} to {totals['settlement_date'].max()}")
    print(f"Requested dates: {start_date} to {end_date}")
    print(f"Rows to update: {update_count}")

    if missing_in_csv:
        print("Dates missing from CSV:", ", ".join(str(day) for day in missing_in_csv))
    if missing_in_xlsx:
        print("Dates missing from XLSX:", ", ".join(str(day) for day in missing_in_xlsx))

    if dry_run:
        print("Dry run: not writing CSV.")
    else:
        df = df.drop(columns=["_date"])
        df.to_csv(csv_path, index=False)
        print("CSV updated.")


def main():
    parser = argparse.ArgumentParser(
        description="Patch aggregate rooftop_GWh values from a rooftop totals XLSX."
    )
    parser.add_argument("start_date", type=parse_date)
    parser.add_argument("end_date", type=parse_date)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.start_date > args.end_date:
        parser.error("start_date must be before or equal to end_date")

    totals = load_rooftop_totals(args.xlsx)
    patch_csv(args.csv, totals, args.start_date, args.end_date, args.dry_run)


if __name__ == "__main__":
    main()
