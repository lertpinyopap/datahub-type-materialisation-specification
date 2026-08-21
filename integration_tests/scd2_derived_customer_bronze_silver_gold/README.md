# SCD2 Derived Customer Bronze Silver Gold

## Purpose

Proves that `change_type: scd2_derived` builds gold SCD2 customer history from
silver source rows whose effective dates come from source business dates.

## Bronze, Silver, Gold

- Bronze is represented by the conceptual raw customer/account source data.
- Silver is represented by the load CSVs in `samples/`. The integration runner
  loads each sample into the scenario source table.
- Gold is the generated `customer_profile` target table with TMS-generated SCD2
  columns.

## What This Test Covers

- `VALID_FROM_DATETIME` is derived from `coalesce(updated_datetime, created_datetime)`.
- `VALID_TO_DATETIME` is calculated from the next derived valid-from value.
- `IS_CURRENT_FLAG` is assigned to the latest version per business key.
- Unchanged reruns do not create duplicate SCD2 rows.
- Historical backfill rows rebuild the affected key window.
- Forward changes close the previous current row and create a new current row.
- Unaffected keys remain current when another key is rebuilt.

## Scenario Shape

The business key is:

```text
CUSTOMER_ID + ACCOUNT_ID
```

The expected target CSVs compare only stable business and SCD2 columns. Generated
surrogate keys, business-key hashes, business-data hashes, and audit timestamps
are intentionally omitted from the expected CSVs.

## Load Steps

- `load_001_initial_gold` loads two initial current customer/account rows.
- `load_002_unchanged_rerun` repeats the same silver rows and expects no target
  change.
- `load_003_historical_backfill` loads an older effective-dated row for `C001`.
  It should become historical and close before the existing `C001` current row.
- `load_004_forward_change` loads a later effective-dated row for `C001`. It
  should become current and close the previous `C001` row.

## Expected End State

The final target table contains:

- `C001/A100` pending from `2024-01-01` to `2024-06-24 23:59:59`.
- `C001/A100` active from `2024-06-25` to `2025-01-14 23:59:59`.
- `C001/A100` suspended from `2025-01-15` and current.
- `C002/A200` active from `2024-07-01` and current.

