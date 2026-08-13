# Implementation Status

The Python reference implementation currently supports a focused subset of the
Type Materialisation Specification.

## Implemented

- Concrete specification parsing through JSON Schema and parse-time semantic
  checks.
- Abstract specification shape validation.
- CSV input validation for local files.
- Custom macro loading through Python macro objects.
- dbt project generation for concrete CSV specifications targeting Snowflake
  staged CSV files.
- dbt project generation for concrete table-source specifications.
- Job event table writes through generated dbt project-level run hooks.
- Quarantine table writes through generated append-only incremental dbt models.
- Generated dbt/Jinja macro files from custom macro objects.
- Optional dbt unit-test YAML generation from a sample CSV file.

## Not Yet Implemented

- SCD1 and SCD2 materialisation.
- `fail_file` validation failure enforcement in generated dbt SQL.
- Uniqueness checks in generated dbt SQL.
- Generated Snowflake file-format objects for CSV stages.
- Python upload of local CSV files to Snowflake stages.
- Date/timestamp format translation from Python `strptime` formats to
  Snowflake formats.
- Custom Python execution inside dbt macros.
- Database-native Python UDF generation.
- Multi-error quarantine output.
