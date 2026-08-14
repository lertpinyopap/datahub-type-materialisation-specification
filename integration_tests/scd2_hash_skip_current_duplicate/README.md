# SCD2 Hash Skip And Update

## Purpose

Proves that continuous SCD2 history uses the configured business data hash to
skip an incoming current duplicate while still versioning real changes.

## What This Test Covers

- `business_data_hash.mode: include` hashes only the configured business field.
- `business_data_hash_duplicate_mode: skip` is explicit in the scenario spec.
- The generated incremental model compares the incoming hash with the current
  target row hash.
- An incoming row with the same business key and same business hash does not
  create a new SCD2 version, even when its selected `valid_from_datetime` is
  newer than the current target row.
- An incoming row with a different hash creates a new current version and
  expires the previous one.
- Changing back to a value with the same hash as an older historical row still
  creates a new version when the current target hash differs.

## Initial State

The test creates a target table with one current active account:

- `A1` has `account_value = 1`.

## Load Steps

- `load_001_source.csv` contains `A1` with `account_value = 1`. This should be
  skipped because the current business hash already matches.
- `load_002_source.csv` contains `A1` with `account_value = 2`. This should
  create a new SCD2 version.
- `load_003_source.csv` contains `A1` with `account_value = 1`. This should
  create another new SCD2 version because the current value is `2`, even though
  the hash matches the original historical row.

## Expected End State

The target table contains three `A1` versions ordered by
`valid_from_datetime`: value `1`, value `2`, then value `1` again. The final
row is current, active, and open-ended.
