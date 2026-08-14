# SCD1 CSV Stage Load

## Purpose

Proves that a materialisation can read a CSV file from a Snowflake stage using
the existing `source.location` spec fields.

## What This Test Covers

- `source.load_method` is omitted, so the source uses the staged CSV path.
- `source.location.stage` is prefixed to the integration stage name, such as
  `TMS_INT__CSV_STAGE`.
- The integration runner creates and tears down the internal stage as test
  setup.
- The generated dbt source model reads from the staged CSV file and loads the
  typed target table.
- The scenario-specific `src/assertions.py` hook can add custom SQL assertions
  after the shared expected-CSV checks.

## Load Steps

`load_001_source.csv` is uploaded to the prefixed stage before `dbt build`
runs. The spec points at `load_001_source.csv` through
`source.location.filename`.

## Expected End State

The target table contains the two staged account rows with
`account_priority` cast to `number(10,0)`. The custom assertion also checks
that the loaded row count is `2` and the priority total is `30`.
