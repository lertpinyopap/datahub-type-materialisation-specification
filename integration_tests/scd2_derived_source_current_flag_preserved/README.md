# scd2_derived_source_current_flag_preserved

This scenario documents the V2/V10 migration shape where historical V2 records
must stay non-current after newer V10 records are loaded for the same business
key.

`scd2_derived` still derives the output `IS_CURRENT_FLAG` from the effective
datetime window. The source `SOURCE_CURRENT_FLAG` is retained as a source field
for traceability, but the gold SCD2 current flag is controlled by the latest
effective record for the business key.

## Checks

- V10 is loaded first as the current source-system row.
- V2 is loaded later but has an older effective datetime.
- The V2 record is inserted as historical and receives `IS_CURRENT_FLAG = N`.
- The V10 record remains current for the shared business key.

