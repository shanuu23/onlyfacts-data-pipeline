"""
One-time script: loads combined_dataset.csv into the Postgres electricity_demand table.
Run this locally once before the daily automation takes over.

Requires environment variables:
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
"""

import os
import pandas as pd
from sqlalchemy import create_engine, text, URL

CSV_PATH = "Data/combined_dataset.csv"
TIME_COLUMNS = [
    "max_operational_time",
    "min_operational_time",
    "max_rooftop_time",
    "min_rooftop_time",
]


def get_engine():
    # URL.create() handles special characters in passwords (e.g. @, #, %)
    # that would break a plain f-string connection string
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


def main():
    print(f"Reading {CSV_PATH}...")
    df = pd.read_csv(
        CSV_PATH,
        parse_dates=["settlement_date", *TIME_COLUMNS],
    ).set_index("settlement_date")
    print(f"  {len(df):,} rows × {len(df.columns)} columns")

    print("\nConnecting to Postgres...")
    engine = get_engine()

    print("Loading into electricity_demand table...")
    # if_exists="replace" drops and recreates the table on each run —
    # safe for a one-time load, ensures schema always matches the CSV exactly.
    df.to_sql(
        name="electricity_demand",
        con=engine,
        if_exists="replace",
        index=True,
        index_label="settlement_date",
    )

    print("Adding primary key on settlement_date...")
    with engine.begin() as conn:
        conn.execute(text("""
            ALTER TABLE electricity_demand
            ADD CONSTRAINT electricity_demand_pkey PRIMARY KEY (settlement_date)
        """))

    print(f"Done. {len(df):,} rows loaded into electricity_demand.")


if __name__ == "__main__":
    main()
