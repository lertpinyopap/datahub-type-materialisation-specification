# Implementation Status

The Python implementation currently supports a focused subset of the Type
Materialisation Specification.

## Implemented

- Concrete specification parsing through JSON Schema and parse-time semantic
  checks.
- Abstract specification shape validation.
- CSV input validation for local files.
- CSV dialect options using Python `csv` names: `delimiter`, `quotechar`,
  `lineterminator`, and `quoting` with `minimal` and `all`.
- Parse-time uniqueness checks for target field ids and specified source column
  names.
- Required `business_key` parsing and validation, with raw or SHA2-256 hashed
  generated key columns and optional generated column-name override.
- Custom macro loading through Python macro objects.
- dbt project generation for concrete CSV specifications targeting Snowflake
  staged CSV files.
- dbt project generation for concrete CSV specifications using dbt seed loading
  from local CSV files.
- dbt project generation for concrete table-source specifications.
- Table-source `query` SQL in generated dbt source models.
- Generated dbt projects reference the user-managed local
  `datahub_type_materialisation` dbt profile.
- `fail_load` validation failure enforcement through generated dbt SQL.
- Field-level uniqueness validation through generated dbt SQL.
- Job event table writes through generated dbt project-level run hooks.
- Quarantine table writes through generated append-only incremental dbt models.
- Generated dbt/Jinja macro files from custom macro objects.
- Optional dbt unit-test YAML generation from a sample CSV file.
- Date/timestamp transform generation for `parse_date` and `parse_timestamp`
  using common Python `strptime` format strings translated to Snowflake format
  strings, with generated parse-failure validation.
- SCD2 auto `business_data_hash` generation from source-derived business
  fields, using the configured generated business key for target joins and
  window partitions.
- SCD2 auto duplicate-hash historical boundary update handling.
- SCD2 auto `valid_from_datetime` and `valid_to_datetime` generation from
  `insert_time`, including configurable earliest-version start-of-time
  handling.
- SCD2 auto dbt validity-window failure guards for `scd2_validation:
  continuous` and `scd2_validation: sparse`.
- SCD2 auto target-aware incremental merge generation.
- Guarded SCD1 and SCD2 auto full rebuilds for
  `delete_detection.mode = truncate`, requiring `allow_truncate: true`.
- SCD2 manual pass-through fields for source-managed validity and state values.
- SCD1 hard-delete filtering for `delete_detection.mode = field`.

## Not Yet Implemented

- Generated Snowflake file-format objects for CSV stages.
- Python upload of local CSV files to Snowflake stages.
- Multi-error quarantine output.
