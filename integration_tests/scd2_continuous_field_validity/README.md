# SCD2 Continuous Field Validity

## Purpose

Proves that generated SCD2 SQL creates continuous validity windows from source
field timestamps. The prior version's `valid_to_datetime` is the next version's
`valid_from_datetime`, not a second before it.

## Initial State

The target table does not exist before the generated dbt project runs.

## Load Steps

`load_001_source.csv` loads two versions for the same account with different
`source_changed_at` values.

## Expected End State

The first row starts at the platform start-of-time timestamp and ends exactly at
the second row's `valid_from_datetime`. The second row is current and ends at the
platform end-of-time timestamp.
