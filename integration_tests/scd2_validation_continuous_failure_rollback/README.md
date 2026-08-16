# SCD2 Continuous Validation Failure Rollback

## Purpose

Proves that a failing `scd2_validation: continuous` check does not create or
update target rows.

## What This Test Covers

- The target starts with a valid current SCD2 row.
- The load attempts to insert a new version at platform end of time.
- The generated validation guard rejects the zero-length validity window.
- The target table remains byte-for-byte equivalent for the asserted business
  and audit columns.

## Load Steps

`load_001_source.csv` contains `A1` with a changed value and an `insert_time` of
`9999-12-31T23:59:59Z`.

## Expected End State

The dbt build fails, and the target still contains only the original seeded row
with its original audit values.
