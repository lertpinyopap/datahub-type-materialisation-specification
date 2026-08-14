# SCD1 Quarantine Regex Validation

## Purpose

Proves that `failure_mode: quarantine_row` writes invalid source rows to the
quarantine table while continuing to load valid rows into the typed target.

## What This Test Covers

- The generated target model filters out rows with validation failures.
- The generated quarantine model captures rows that fail validation.
- A regex validation on `account_id` rejects malformed source data.
- The default quarantine table name uses the target table name plus
  `__QUARANTINE`.

## Load Steps

`load_001_source.csv` contains three valid account ids and one malformed account
id, `BAD3`, which does not match `^A[0-9]{3}$`.

## Expected End State

The target table contains `A001`, `A002`, and `A004`. The quarantine table
contains the rejected `BAD3` row with the validation failure detail.
