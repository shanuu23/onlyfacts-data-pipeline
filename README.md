# OnlyFacts Data Pipeline

Automated data pipelines for OnlyFacts.

## Electricity demand

The existing electricity-demand pipeline is located in `electricity_demand/`.
GitHub Actions runs it daily at 17:00 UTC using
`.github/workflows/electricity-demand.yml`.

Run the daily pipeline from the repository root:

```bash
python electricity_demand/daily_pipeline.py
```

Backfill an inclusive historical date range without writing to Postgres:

```bash
python electricity_demand/backfill_daily.py 2026-06-17 2026-06-29 --dry-run
```

The Postgres connection uses these environment variables:

- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`

Downloaded and generated data is stored under `electricity_demand/data/` and
is excluded from Git.

## NTESMO generation mix

The NTESMO pipeline is being added incrementally. The offline transformation
can be tested against an original weekly generation-mix file without network or
database access:

```bash
python ntesmo/daily_pipeline.py --file /path/to/weekly.csv --dry-run
```

Test discovery and download of the latest first-page source without database
writes:

```bash
python ntesmo/daily_pipeline.py --dry-run
```

A known direct asset URL can also be checked without database writes:

```bash
python ntesmo/daily_pipeline.py --url "https://ntesmo.com.au/path/to/file.csv" --dry-run
```

The NTESMO GitHub workflow is currently a read-only source check. Database
writes and the Monday schedule will be added only after GitHub Actions can
download the public source successfully.
