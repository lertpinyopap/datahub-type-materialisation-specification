# SCD2 Missing From Source Delete

## Purpose

Proves that `delete_detection.mode: missing_from_source` compares the incoming
snapshot with current target rows and creates a logical delete version for a
current business key absent from the load.

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
