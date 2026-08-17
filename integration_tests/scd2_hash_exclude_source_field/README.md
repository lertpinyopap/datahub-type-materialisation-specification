# SCD2 Hash Exclude Source Field

## Purpose

Proves that `control_data.business_data_hash.business_data_hash_mode: exclude`
removes the listed target field from SCD2 auto change detection.

## What This Test Covers

- `business_data_hash` is calculated from eligible business-data fields with a
  `|` separator before SHA2 hashing.
- A configured excluded field is not part of `business_data_hash`.
- Changing only an excluded field does not create a new SCD2 version.
- The expected `BUSINESS_DATA_HASH` value is the SHA2-256 hash of `A1|1`.

## Initial State

The target starts with `A1`, `account_value = 1`, and
`source_batch_id = batch_001`.

## Load Steps

- `load_001_source.csv` provides the same `account_id` and `account_value`, but
  changes `source_batch_id` to `batch_002`.

## Expected End State

The target still contains one current row. The `source_batch_id` remains
`batch_001` because the incoming row is a duplicate under the configured
business-data hash.
