# scd2_derived_same_effective_datetime_dedup

This scenario verifies that `scd2_derived` can deterministically collapse duplicate
source versions that share the same business key and effective datetime.

The bronze source can emit more than one customer state for the same effective
date. The silver load carries an `INGEST_SEQUENCE` value, and the spec uses
`scd.deduplicate.order_by` so the highest sequence wins before the gold SCD2
window is calculated.

## Checks

- Two source rows arrive for the same `CUSTOMER_ID`, `ACCOUNT_ID`, and effective date.
- `INGEST_SEQUENCE desc` selects the winning silver version.
- The gold table has only one SCD2 version for that effective datetime.
- The chosen version remains current with an open-ended `VALID_TO_DATETIME`.

