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
- Required `business_key.fields` parsing and validation, with fixed SHA2-256
  generated business-key columns that can be skipped by spec.
- Default id-derived surrogate-key GUID columns that can be skipped by spec.
- Custom macro loading through Python macro objects.
- dbt project generation for concrete CSV specifications targeting Snowflake
  staged CSV files.
- dbt project generation for concrete CSV specifications using dbt seed loading
  from local CSV files.
- dbt project generation for concrete table-source specifications.
- Table-source `query` SQL in generated dbt source models.
- Field-source defaults with `default_value`, `default_from_field`, and
  `fixed_value`, including standalone generated values for table sources.
- Table-source field lookups with `lookup.reference_entity`,
  `lookup.reference_attribute`, `lookup.source_expression`, and generated
  current/non-deleted reference joins.
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
- Guarded full rebuilds for `truncate_before_load: true`, requiring
  `allow_truncate: true`.
- SCD2 manual pass-through fields for source-managed validity and state values.
- SCD2 derived target-aware incremental merge generation for table sources where
  source effective timestamps drive generated SCD2 windows.
- SCD2 derived affected-key derivation with affected-window validation using the
  previous-and-next window strategy.
- SCD2 derived generated metadata columns:
  `is_current_flag`, `is_deleted_flag`, `valid_from_datetime`,
  `valid_to_datetime`, `business_data_hash`, and audit fields.
- SCD2 derived same-effective-datetime deduplication through
  `scd.deduplicate.order_by`.
- SCD2 derived integration scenarios for unchanged reruns, historical backfills,
  out-of-order Bronze/Silver/Gold loads, middle-row insertion, and V2/V10
  historical loads.
- SCD1 hard-delete filtering for `delete_detection.mode = field`.
- Declarative target and column tags rendered as Snowflake `post_hook` statements
  after the generated target relation is materialized.
- Declarative target and column tags rendered as Snowflake `post_hook`
  statements after the generated target relation is materialized.

## Not Yet Implemented

- Generated Snowflake file-format objects for CSV stages.
- Python upload of local CSV files to Snowflake stages.
- Multi-error quarantine output.
- SCD2 derived source-current-flag override mode. Current generated
  `is_current_flag` is derived from the latest effective window per business key.
