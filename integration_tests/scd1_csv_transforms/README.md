# SCD1 CSV Transforms

## Purpose

Proves that generated dbt SQL applies common field transforms while loading CSV
source data into a typed table.

## What This Test Covers

- CSV seed-backed SCD1 table generation.
- `trim` with the default `both` behavior.
- `trim` with `side: left`.
- `trim` with `side: right`.
- `round` to a fixed decimal scale.
- Strict whitespace comparison for this scenario, so missing trim transforms do
  not get hidden by tolerant row comparison.

## Initial State

The target table does not exist before the generated dbt project runs.

## Load Steps

`load_001_source.csv` includes values with leading and trailing whitespace plus
numeric values that require rounding.

## Expected End State

The target table contains trimmed string fields and rounded numeric fields.
