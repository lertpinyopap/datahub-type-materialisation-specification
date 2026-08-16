# SCD2 Missing From Source Is Ignored

## Purpose

Proves that simplified SCD2 auto does not infer logical deletes for current
business keys absent from the load.

## What This Test Covers

- dbt project generation for a `table` SCD2 auto spec using `insert_time`.
- Generated dbt unit-test fixtures for the source and validation guard models.
- Generated dbt unit-test override of `is_incremental: false`, so the dbt unit
  test validates first-load transformation behavior without reading `{{ this }}`.
- Live incremental behavior against an existing target table without
  synthesizing missing-from-source delete rows.
- Unchanged current-key behavior: a present row with the same business hash
  remains current and active.

## Initial State

The test creates a target table with two current accounts:

- `A1` is current and active.
- `A2` is current and active.

## Load Steps

`load_001_source.csv` contains only `A2`. Since SCD2 auto no longer has
missing-from-source delete detection, the generated dbt model leaves `A1`
unchanged.

## Expected End State

`A1` remains the unchanged current active row. `A2` remains the unchanged
current active row.
