# Implementation Status

The Python reference implementation currently supports a focused subset of the
Type Materialisation Specification.

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
- Generated dbt projects reference the user-managed local
  `datahub_type_materialisation` dbt profile.
- `fail_file` validation failure enforcement through generated dbt SQL.
- Field-level uniqueness validation through generated dbt SQL.
- Job event table writes through generated dbt project-level run hooks.
- Quarantine table writes through generated append-only incremental dbt models.
- Generated dbt/Jinja macro files from custom macro objects.
- Optional dbt unit-test YAML generation from a sample CSV file.

## Not Yet Implemented

- SCD1 and SCD2 materialisation.
- Generated Snowflake file-format objects for CSV stages.
- Python upload of local CSV files to Snowflake stages.
- Date/timestamp format translation from Python `strptime` formats to
  Snowflake formats.
- Multi-error quarantine output.
