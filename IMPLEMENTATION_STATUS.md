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
- SCD2 `business_data_hash` generation from selected source-derived business
  fields.
- SCD2 duplicate-hash historical boundary handling for
  `business_data_hash_duplicate_mode = skip` and `update`.
- SCD2 current-load `valid_from_datetime` and `valid_to_datetime` generation,
  including continuous per-business-key windows when
  `valid_to_datetime_selection` is `next`.
- SCD2 current-load `is_current_flag` and `is_deleted_flag` generation for
  `delete_detection.mode = never` and `delete_detection.mode = field`.
- SCD2 target-aware incremental merge generation for
  `delete_detection.mode = missing_from_source`, including generated logical
  delete rows for current target keys absent from the incoming load.
- SCD1 hard-delete filtering for `delete_detection.mode = field`.

## Not Yet Implemented

- Generated Snowflake file-format objects for CSV stages.
- Python upload of local CSV files to Snowflake stages.
- Multi-error quarantine output.
