# SCD1 Table Source Query Quarantine

## Purpose

Proves that `failure_mode: quarantine_row` operates on the result of
`source.query`, not on every row or column in the backing source table.

## What This Test Covers

- The live runner creates a prefixed table source with every source column as
  `varchar`.
- The generated source model wraps a full SQL `source.query`.
- The query filters to `LOAD_001`.
- The query selects only business columns and drops the `load_batch_id` control
  column.
- Target loading and quarantine both use the query result.
- A malformed row in `LOAD_001` is quarantined.
- A malformed row in `LOAD_999` is ignored because it is outside the query
  result.

## Initial State

The target table, source table, and quarantine table do not exist before the
generated dbt project runs.

## Load Steps

`load_001_source.csv` is loaded into the prefixed source table before
`tms dbt-build` runs. It contains valid and malformed rows for `LOAD_001` and
`LOAD_999`.

## Expected End State

The target table contains only valid `LOAD_001` rows. The quarantine table
contains only the malformed `LOAD_001` row and does not expose the source
table's `load_batch_id` control column.
