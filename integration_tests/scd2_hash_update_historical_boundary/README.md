# SCD2 Hash Update Historical Boundary

## Purpose

Proves that `business_data_hash_duplicate_mode: update` can extend an existing
SCD2 version by moving a duplicate-hash historical boundary rather than leaving
two contiguous versions with the same business data hash.

## What This Test Covers

- `business_data_hash.mode: include` hashes only the configured business field.
- `business_data_hash_duplicate_mode: update` is explicit in the scenario spec.
- An incoming historical row whose hash matches the next existing version moves
  that next version's `valid_from_datetime` backward.
- The generated incremental model replaces all rows for the affected business
  key, so the old boundary row is not left behind.

## Initial State

The test creates a target table with two versions for `A1`:

- `account_value = 1` from platform start of time to `2026-09-03`.
- `account_value = 2` from `2026-09-03` to platform end of time.

## Load Steps

`load_001_source.csv` contains `A1` with `account_value = 2` and an explicit
`valid_from_datetime` of `2026-09-02T00:00:00Z`.

## Expected End State

The value `2` row starts at `2026-09-02 00:00:00`. The old value `2` row that
started at `2026-09-03 00:00:00` is not retained.
