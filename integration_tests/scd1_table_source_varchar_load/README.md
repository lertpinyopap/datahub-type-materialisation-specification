# SCD1 Table Source VARCHAR Load

## Purpose

Proves that a generated dbt project can read from an existing table source
instead of a CSV seed or stage.

## What This Test Covers

- The live runner creates a prefixed source table before dbt runs.
- The source table is loaded from CSV with every source column declared as
  `varchar`.
- The generated table-source model reads from the prefixed source table.
- The final typed table is populated from the source table, including casting a
  VARCHAR source value into a numeric target column.

## Initial State

The target table and source table do not exist before the generated dbt project
runs. The live runner creates the source table as part of the load step.

## Load Steps

`load_001_source.csv` is loaded into the prefixed source table before
`tms dbt-build` runs.

## Expected End State

The target table contains the two source accounts with `account_priority`
materialised as the typed numeric target field.
