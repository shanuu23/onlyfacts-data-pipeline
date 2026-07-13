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
