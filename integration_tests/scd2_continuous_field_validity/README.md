# SCD2 Continuous Field Validity

## Purpose

Proves that generated SCD2 auto SQL creates the first continuous validity window
from the configured `insert_time`.

## What This Test Covers

- dbt project generation for a `table` SCD2 auto spec using `insert_time`.
- Generated dbt unit-test fixtures for the source and validation guard models.
- First-load SCD2 auto behavior.
- Continuous window calculation: the first row starts at platform start of time
  and ends at platform end of time.
- Current/delete flags: the row is current and not deleted.

## Initial State

The target table does not exist before the generated dbt project runs.

## Load Steps

`load_001_source.csv` loads one account row.

## Expected End State

The row starts at the platform start-of-time timestamp, is current, and ends at
the platform end-of-time timestamp.
