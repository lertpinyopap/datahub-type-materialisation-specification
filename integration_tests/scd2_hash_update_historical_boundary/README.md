# SCD2 Hash Update Historical Boundary

## Purpose

Proves that SCD2 auto can extend an existing version by moving a duplicate-hash
historical boundary rather than leaving two contiguous versions with the same
business data hash.

## What This Test Covers

- Generated `business_data_hash` values drive duplicate-boundary handling.
- An incoming historical row whose hash matches the next existing version moves
  that next version's `valid_from_datetime` backward.
- The generated incremental model replaces all rows for the affected business
  key, so the old boundary row is not left behind.

## Initial State

The test creates a target table with two versions for `A1`:

- `account_value = 1` from platform start of time through the instant before
  `2026-09-03`.
- `account_value = 2` from `2026-09-03` to platform end of time.

## Load Steps

`load_001_source.csv` contains `A1` with `account_value = 2` and an `insert_time`
of `2026-09-02T00:00:00Z`.

## Expected End State

The value `2` row starts at `2026-09-02 00:00:00`. The old value `2` row that
started at `2026-09-03 00:00:00` is not retained.
