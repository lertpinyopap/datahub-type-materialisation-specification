# SCD1 CSV Date And Timestamp Formats

## Purpose

Proves that generated dbt SQL translates Python `strptime` date and timestamp
formats into Snowflake parsing formats.

## What This Test Covers

- `parse_date` for ISO dates.
- `parse_date` for Australian day/month/year dates.
- `parse_date` for abbreviated month-name dates.
- `parse_timestamp` with a `%z` timezone offset.
- `parse_timestamp` with `timezone_if_missing: UTC`.

## Initial State

The target table does not exist before the generated dbt project runs. The
runner uploads the headerless CSV file to the scenario's prefixed Snowflake
stage so dbt parses the raw source strings.

## Load Steps

`load_001_source.csv` contains two accounts with the same logical dates and
timestamps represented in several source string formats.

## Expected End State

The target table contains typed `date` and `timestamp_tz` values represented in
the runner's normalized comparison format.
