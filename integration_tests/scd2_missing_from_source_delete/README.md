# SCD2 Missing From Source Delete

## Purpose

Proves that `delete_detection.mode: missing_from_source` compares the incoming
snapshot with current target rows and creates a logical delete version for a
current business key absent from the load.

## What This Test Covers

- dbt project generation for a `table` SCD2 spec using
  `delete_detection.mode: missing_from_source`.
- Generated dbt unit-test fixtures for the source and validation guard models.
- Generated dbt unit-test override of `is_incremental: false`, so the dbt unit
  test validates first-load transformation behavior without reading `{{ this }}`.
- Live incremental behavior against an existing target table, including querying
  current target rows to discover keys missing from the incoming source.
- Logical delete behavior: the absent current key gets a new current deleted
  version, and the previous active version is expired.
- Unchanged current-key behavior: a present row with the same business hash
  remains current and active.

## Initial State

The test creates a target table with two current accounts:

- `A1` is current and active.
- `A2` is current and active.

## Load Steps

`load_001_source.csv` contains only `A2`. Since `A1` is absent from the incoming
snapshot, the generated dbt model must expire the active `A1` row and create a
new current deleted `A1` version.

## Expected End State

`A1` has one expired active row and one current deleted row. `A2` remains the
unchanged current active row.
