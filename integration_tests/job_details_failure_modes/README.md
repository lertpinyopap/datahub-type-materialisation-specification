# Job Details Failure Modes

## Purpose

Proves that failed dbt builds still write lifecycle rows to the job details
table and that the `DETAILS` value distinguishes validation failures from other
runtime dbt failures.

## What This Test Covers

- `failure_mode: fail_load` fails the dbt build when validation errors exist.
- The validation-guard failure writes `RESULT = FAILED` with validation-specific
  details.
- A later runtime failure caused by a deliberately broken generated final model
  also writes `RESULT = FAILED`, but with runtime failure details.
- Job rows accumulate across load attempts inside the same scenario run.

## Load Steps

- `load_001_source.csv` contains `BAD3`, which fails the `account_id` regex
  validation.
- `load_002_source.csv` contains valid source data, but the scenario runner
  deliberately replaces the generated final model with a query against a
  missing relation before the second dbt build, causing a serious runtime
  failure.

## Expected End State

After the first load, the job table contains one `JOB_START` and one failed
`JOB_END` row. After the second load, the job table contains two starts and two
failed ends, with different `DETAILS` values for validation and runtime
failure.
