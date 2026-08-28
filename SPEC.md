# Type Materialisation Specification

## 1. Purpose

The Type Materialisation Specification defines a generic YAML document format
for describing how flat, raw, or schema-on-read data is transformed into typed
relational data.

The specification is designed to support CSV sources, table sources, reusable
table families through inheritance, and dbt-based materialisation, and may be
extended to other source formats in the future.

## 2. Notation

This document uses a small grammar-like notation for the shape of the YAML:

```text
name ::= allowed_value | other_allowed_value
property? ::= optional property
property[] ::= list property
```

The grammar is descriptive. The formal static validator for concrete
specifications is the JSON Schema in
`schema/type-materialisation.schema.json`. Abstract specifications used for
inheritance can be shape-checked with
`schema/type-materialisation-abstract.schema.json`.

Unless otherwise stated, a required string value must contain at least one
non-whitespace character after YAML parsing. Optional string values follow the
same rule when present.

Identifiers are a constrained form of string used for stable specification and
field identity:

```text
identifier ::= string matching ^[A-Za-z][A-Za-z0-9_-]*$
```

An identifier starts with an ASCII letter and may then contain ASCII letters,
digits, underscores, or hyphens.

Identifiers are compared case-insensitively to align with database identifier
handling. For example, `account_1` and `ACCOUNT_1` identify the same
specification or field.

When an identifier or string value is materialised as a physical database,
schema, table, stage, or column name, implementations must emit the physical
name in uppercase. This includes generated names derived from ids, configured
relation names, inferred CSV column names, generated audit columns, job table
columns, and quarantine table columns. Runtime overrides that supply physical
names should be normalized to uppercase before dbt resolves or logs them.

All `database` properties in this specification are optional. When a database is
omitted, implementations must let the active dbt adapter and database session
context for the job resolve it. Implementations must not silently substitute a
different configured database. Production specifications should provide
database values explicitly unless they intentionally rely on runtime context.

All timestamp and datetime values in this specification must be timezone-aware.
Implementations must reject timestamp values without timezone
information unless a specific rule, such as a `parse_timestamp`
`timezone_if_missing` policy, explicitly states how the missing timezone is to
be interpreted.

## 3. Overall Document

```text
type_materialisation_spec ::= complete_spec | abstract_spec | extending_spec

complete_spec ::=
  id
  description?
  control_data?
  source
  target

abstract_spec ::=
  id
  description?
  control_data?
  source?
  target?

extending_spec ::=
  id
  description?
  extends  # see section 15, Inheritance
  control_data
  source?
  target?
```

Sample: complete CSV-backed materialisation specification.

```yaml
id: account
description: Maps raw account CSV data into a typed account table.
control_data:
  materialisation_type: table
  change_type: scd1
  failure_mode: quarantine_row
source:
  format: csv
  delimiter: ","
  header: true
target:
  id: account
  database: analytics
  schema: business
  fields:
    - id: account_id
      source:
        pos: 0
        column: account_id
      data_type: varchar(20)
      nullable: false
      unique: true
    - id: account_name
      source:
        pos: 1
        column: account_name
      data_type: varchar(255)
      transforms:
        - type: trim
      nullable: true
    - id: opened_on
      source:
        pos: 2
        column: opened_on
      data_type: date
      transforms:
        - type: parse_date
          format: "%d/%m/%Y"
      nullable: false
    - id: balance
      source:
        pos: 3
        column: balance
      data_type: decimal(18,2)
      transforms:
        - type: round
          scale: 2
          mode: half_up
      nullable: false
```

An abstract specification is incomplete by itself and is not directly
materialisable. It may omit any optional or required element from a complete
specification so that descendants can supply the missing attributes through
inheritance. Any element that an abstract specification does include must still
be valid for its declared shape.

## 4. Identity

```text
id ::= identifier
description ::= string
extends ::= identifier  # see section 15, Inheritance
```

`id` is the stable identifier for the specification.

`description` is human-readable documentation for the specification.

`extends` identifies a parent specification to inherit from. An extending
specification may be partial because missing attributes can be inherited from
the parent. Inheritance is resolved after the base document parses; see
[section 15](#15-inheritance).

Sample: specification identity with inheritance.

```yaml
id: customer_reference_code_data
description: Customer reference code data.
extends: abstract_reference_code_data
```

## 5. Control Data

```text
control_data ::=
  change_type
  business_key?
  skip_business_key?
  skip_surrogate_key?
  business_data_hash?
  materialisation_type?
  failure_mode?
  truncate_before_load?
  staging_schema?
  scd?
  quarantine?
  job?

materialisation_type ::= table
failure_mode ::= fail_load | quarantine_row
change_type ::= scd1 | scd2_manual | scd2_auto | scd2_derived
truncate_before_load ::= true | false
business_key ::= business_key_config
skip_business_key ::= true | false
skip_surrogate_key ::= true | false
business_data_hash ::= business_data_hash_config
staging_schema ::= string
scd ::= scd_config
quarantine ::= quarantine_table
job ::= job_table
```

`control_data` defines behavior for the whole materialisation.

`materialisation_type` defines the kind of typed output to produce. If omitted,
implementations should default to `table`. The current specification only
supports table materialisations.

`failure_mode` defines how validation or conversion failures are handled. If
omitted, implementations should default to `fail_load`.

`change_type` explicitly declares the change-handling behavior. It is required
for complete specifications and has no default.

`truncate_before_load` controls whether the target output is fully rebuilt from
the generated source output before loading. If omitted, implementations should
default to `false`. When `true`, dbt implementations must require an explicit
runtime variable `allow_truncate: true` before executing the rebuild. This
second opt-in is a safety precaution so a specification cannot accidentally
trigger destructive target replacement without an operator explicitly enabling it
for that run.

`business_key` declares the target fields used to identify the business entity
represented by each record. It is required unless `skip_business_key` is `true`.
For `scd2_auto`, `business_key` is required even when the generated
business-key column is skipped because the fields are needed for history
windowing and target matching.

`skip_business_key` controls whether the generated business-key column is
omitted from the target output. If omitted, implementations should default to
`false`.

`skip_surrogate_key` controls whether the generated surrogate-key column is
omitted from the target output. If omitted, implementations should default to
`false`.

`staging_schema` declares the default schema for generated source-record,
staging, intermediate, validation guard, seed, and quarantine relations that are
not the core target output and are not job control tables. If omitted,
implementations should default to `INTERMEDIATE`. dbt implementations should
allow this default to be overridden at runtime with the `tms_staging_schema`
variable.

`scd` configures delete handling for SCD1, update behavior for `scd2_manual`,
generated validity-window behavior and protected full-history rebuilds for
`scd2_auto`, and source-effective-date validity-window derivation for
`scd2_derived`.

`quarantine` optionally describes the quarantine table used when
`failure_mode` is `quarantine_row`.

`job` optionally describes the job table used to record load lifecycle events.

`materialisation_type` values:

- `table`: produce a typed table populated from the source data.

`failure_mode` values:

- `fail_load`: fail the whole materialisation load when a record fails validation or
  type conversion.
- `quarantine_row`: write the failing record to quarantine output and continue
  processing subsequent records.

`change_type` values:

- `scd1`: materialise the current typed state without SCD2 history metadata.
- `scd2_manual`: copy SCD2 validity and state fields directly from the source.
- `scd2_auto`: preserve historical versions using implementation-managed
  validity windows.
- `scd2_derived`: preserve historical versions using source-derived effective
  datetimes and implementation-managed SCD2 metadata.

Sample: control data.

```yaml
control_data:
  materialisation_type: table
  failure_mode: quarantine_row
  change_type: scd2_auto
  truncate_before_load: false
  staging_schema: INTERMEDIATE
  business_key:
    fields:
      - account_id
  business_data_hash:
    business_data_hash_mode: exclude
    fields:
      - source_loaded_at
  scd:
    insert_time: "{{ var('insert_time') }}"
  quarantine:
    table: account__QUARANTINE
  job:
    schema: BUSINESS
    table: TYPE_MATERIALISATION_JOBS
```

### 5.1 Business Key

```text
business_key_config ::=
  fields[]

fields ::= target field id
```

`business_key` declares the ordered list of target field ids used to build the
generated business-key value. Every field id must exist in `target.fields`.

When `skip_business_key` is omitted or `false`, implementations must generate a
business-key column on the target. The generated column is physically stored as
`varchar`. Its generated column name is `<target.id>_BUSINESS_KEY`, using the
implementation's normal physical identifier casing. For example, a target id of
`customer_mapping` produces `CUSTOMER_MAPPING_BUSINESS_KEY`.

The generated business-key value is always the SHA2-256 hash of the configured
field values concatenated with the pipe character `|`. The business-key column
name, hashing algorithm, and separator are fixed by the specification and are
not configurable. If `target.id` contains an implementation or environment
prefix separated by `__`, generated business-key column names use only the part
after the final `__`.

When `skip_business_key: true`, implementations must omit the generated
business-key column from the target output.

Sample: generated business key.

```yaml
control_data:
  change_type: scd1
  business_key:
    fields:
      - account_id
```

Sample: skipped generated business key.

```yaml
control_data:
  change_type: scd1
  skip_business_key: true
```

### 5.2 Surrogate Key

```text
skip_surrogate_key ::= true | false
```

When `skip_surrogate_key` is omitted or `false`, implementations must generate
a unique row-level GUID value for every newly created target row. The generated
surrogate-key column is physically stored as `varchar(36)`.

The generated surrogate-key column name is `<target.id>_KEY`, using the
implementation's normal physical identifier casing. For example, a target id of
`customer_mapping` produces `CUSTOMER_MAPPING_KEY`. If `target.id` contains an
implementation or environment prefix separated by `__`, generated surrogate-key
column names use only the part after the final `__`.

When `skip_surrogate_key: true`, implementations must omit the generated
surrogate-key column from the target output.

The generated surrogate-key column is system metadata. It must not be declared
in `target.fields`, must not be listed in `business_key.fields`, and must be
excluded from `business_data_hash`.

Sample: default surrogate key generation.

```yaml
control_data:
  change_type: scd1
  business_key:
    fields:
      - account_id
```

Sample: skipped surrogate key generation.

```yaml
control_data:
  change_type: scd1
  business_key:
    fields:
      - account_id
  skip_surrogate_key: true
```

### 5.3 Business Data Hash

```text
business_data_hash_config ::=
  skip_business_data_hash?
  business_data_hash_mode?
  fields[]?

skip_business_data_hash ::= true | false
business_data_hash_mode ::= exclude | include
fields ::= target field id
```

When `business_data_hash` is omitted, or when `skip_business_data_hash` is
omitted or `false`, implementations must generate a `business_data_hash` column
on the target output. The generated column is physically stored as
`varchar(64)`.

The generated value is the SHA2-256 hash of the selected business-data field
values concatenated with the pipe character `|`. Null values are treated as
empty strings for concatenation. The column name, hashing algorithm, and
separator are fixed by the specification and are not configurable.

By default, all eligible target fields are included. Generated surrogate-key
values, generated business-key values, generated audit metadata, generated
`scd2_auto` metadata, and `scd2_manual` validity/state fields are not eligible.
For `scd2_manual`, fields referenced by `scd.update_key.fields` are also not
eligible. The source fields used to generate the business key remain eligible
business-data fields.

When `skip_business_data_hash: true`, implementations must omit the generated
`business_data_hash` column from the target output.

`business_data_hash_mode` values:

- `exclude`: hash all eligible target fields except those listed in `fields`.
  This is the default when `business_data_hash_mode` is omitted. When `fields`
  is omitted, no eligible fields are excluded.
- `include`: hash only the eligible target fields listed in `fields`. `fields`
  must contain at least one field.

For `business_data_hash_mode: include`, every listed field must be eligible.
The include list must not reference generated surrogate-key columns, generated
business-key columns, `scd2_auto` metadata columns, `scd2_manual`
validity/state fields, or `scd.update_key.fields`.

Sample: default business data hash.

```yaml
control_data:
  change_type: scd1
  business_key:
    fields:
      - account_id
```

Sample: skipped business data hash.

```yaml
control_data:
  change_type: scd1
  business_key:
    fields:
      - account_id
  business_data_hash:
    skip_business_data_hash: true
```

Sample: include selected business data hash fields.

```yaml
control_data:
  change_type: scd1
  business_key:
    fields:
      - account_id
  business_data_hash:
    business_data_hash_mode: include
    fields:
      - account_name
      - account_status
```

Sample: exclude selected business data hash fields.

```yaml
control_data:
  change_type: scd2_manual
  business_key:
    fields:
      - account_id
  business_data_hash:
    business_data_hash_mode: exclude
    fields:
      - source_loaded_at
```

### 5.4 Quarantine Table

```text
quarantine_table ::=
  database?
  schema?
  table?

database ::= string
schema ::= string
table ::= string
```

The quarantine table is used when `failure_mode` is `quarantine_row`.

In generated-object defaulting rules, the resolved target database, schema, and
table are the physical relation location declared by the resolved target or
resolved by the active dbt adapter and session context.
The resolved target table name is `target.table_name` when supplied, otherwise
`target.id`.

If `quarantine.database` is omitted, implementations should use
`target.database` when supplied, otherwise the active dbt adapter and database
session context.

If `quarantine.schema` is omitted, implementations should default it to the
resolved staging schema.

If `quarantine.table` is omitted, implementations should default it to the
resolved target table name with `__QUARANTINE` appended.

The quarantine table must be created if it does not exist. If it already exists,
it must be expanded to cover the number of source fields present in the source
data, but it must never be shrunk automatically.

Each quarantined row contains quarantine metadata followed by the source data in
source order.

The quarantine metadata columns are:

- `loaded_at`: the time the row was loaded.
- `job_id`: the identifier for the materialisation job. This references the
  `job_id` shared by the start and end events in the job table.
- `failure_details`: details of the first failure detected for the row.

For convenience, quarantine metadata columns appear first in the quarantine
table. Source data columns follow in file order for CSV sources and table column
order for table sources.

Future versions may add all failure details to the quarantine output to support
faster issue resolution.

Sample: quarantine table configuration.

```yaml
control_data:
  change_type: scd1
  failure_mode: quarantine_row
  quarantine:
    database: ops
    schema: data_quality
    table: account_load__QUARANTINE
```

### 5.5 Job Table

```text
job_table ::=
  database?
  schema?
  table?

database ::= string
schema ::= string
table ::= string

job_event_type ::= JOB_START | JOB_END
job_result ::= COMPLETED | COMPLETED_WITH_QUARANTINE | FAILED
  | FAILED_TO_PARSE | FAILED_TO_INHERIT | FAILED_TO_RESOLVE
  | FAILED_TO_COMPILE
```

The job table records event rows for each materialisation load. Each load emits
a `JOB_START` event and a `JOB_END` event. The two events for the same load
must share the same `job_id`.

If `job.table` is omitted, implementations should default to
`TYPE_MATERIALISATION_JOBS`.

If `job.database` is omitted, implementations should use `target.database` when
supplied, otherwise the active dbt adapter and database session context.

If `job.schema` is omitted, implementations should default it to `BUSINESS`.

The job table must be created if it does not exist. If it already exists, it
must not be altered automatically.

The job event columns are:

- `job_id`: the GUID identifier for a single materialisation load. This is
  generated by the implementation from the dbt invocation id, is shared by the
  load's `JOB_START` and `JOB_END` events, and is referenced by quarantine rows
  for failures from the same load. It must not be supplied or overridden by the
  caller.
- `event_type`: the lifecycle event type.
- `event_timestamp`: the timezone-aware time the event occurred. The physical
  data type is `timestamp_tz`.
- `result`: the load result. `JOB_START` events should leave this value null
  unless the materialisation fails before load time. `JOB_END` events must set
  it to `COMPLETED`, `COMPLETED_WITH_QUARANTINE`, or `FAILED`.
- `details`: nullable free-form detail text for validation failures, runtime
  failures, or other diagnostic context.
- `spec_file_name`: nullable specification file name used to generate the dbt
  artifacts for the load.
- `generated_table`: nullable fully qualified generated output table name.
- `quarantine_table`: nullable fully qualified quarantine table name, when a
  quarantine table is configured.
- `loaded_count`: nullable count of records written to the generated output
  table. `JOB_START` events must leave this value null. `JOB_END` events should
  set it when the generated output table exists.
- `quarantine_count`: nullable count of records written to the quarantine
  table. `JOB_START` events must leave this value null. `JOB_END` events should
  set it when a quarantine table is configured and exists.
- `audit_data_process_key`: the operational process key for the pipeline
  execution or run that produced the job event.
- `audit_created_datetime`: the timezone-aware time the job event row was first
  created in the platform.
- `audit_last_changed_datetime`: the timezone-aware time the job event row was
  most recently changed in the platform.

The context columns `details`, `spec_file_name`, `generated_table`, and
`quarantine_table` and the count columns `loaded_count` and `quarantine_count`
must be nullable so existing job history remains valid when the job table
structure evolves. Audit metadata columns must use the generated metadata data
types defined in [section 7.1](#71-target-audit-metadata).

`job_event_type` values:

- `JOB_START`: the materialisation load has started.
- `JOB_END`: the materialisation load has ended.

`job_result` values:

- `COMPLETED`: the materialisation load completed without validation,
  conversion, or runtime failures.
- `COMPLETED_WITH_QUARANTINE`: the materialisation load completed after writing
  one or more failed records to quarantine output.
- `FAILED`: the materialisation load did not complete successfully.
- `FAILED_TO_PARSE`: the materialisation failed during schema-time or
  parse-time validation.
- `FAILED_TO_INHERIT`: the materialisation failed while loading or overlaying
  inherited specifications.
- `FAILED_TO_RESOLVE`: the materialisation failed while resolving variables or
  other pre-load values.
- `FAILED_TO_COMPILE`: the materialisation failed while generating or compiling
  dbt artifacts from a resolved specification.

The quarantine table retains its own `loaded_at`, `job_id`, and
`failure_details` metadata columns so failed rows can be inspected directly
while still tying back to the exact load lifecycle details in the job table.

The `audit_data_process_key` links job events to centralized operational
metadata for lineage tracing, reconciliation, and observability. The same
`audit_data_process_key` must be applied to target rows created or changed by
the materialisation process. Unlike `job_id`, `audit_data_process_key` is
supplied by the calling orchestration or operational metadata system.

Sample: job table configuration.

```yaml
control_data:
  change_type: scd1
  job:
    database: ops
    schema: BUSINESS
    table: TYPE_MATERIALISATION_JOBS
```

### 5.6 Slowly Changing Dimensions

```text
scd_config ::= scd1_scd_config | scd2_manual_scd_config | scd2_auto_scd_config | scd2_derived_scd_config

scd1_scd_config ::=
  delete_detection?

scd2_manual_scd_config ::=
  update_mode?
  update_key?

scd2_auto_scd_config ::=
  insert_time
  scd2_auto_from_sot?
  scd2_validation?

scd2_derived_scd_config ::=
  valid_from_datetime
  valid_to_datetime?
  current_flag?
  deleted_flag?
  derivation_scope?
  validation_scope?
  window_strategy?
  deduplicate?

delete_detection ::=
  mode: field
    field
    value

insert_time ::= scalar
scd2_auto_from_sot ::= true | false
scd2_validation ::= continuous | sparse
valid_from_datetime ::=
  source_column | expression
source_column ::= target field id or generated source alias
expression ::= SQL expression evaluated against generated source rows
update_mode ::= append_only | upsert
update_key ::=
  fields[]
delete_detection_mode ::= field
field ::= target field id
value ::= scalar
```

SCD configuration is used for delete handling when `change_type` is `scd1`, for
manual update behavior when `change_type` is `scd2_manual`, for generated
validity-window behavior and protected full-history rebuilds when `change_type`
is `scd2_auto`, and for source-effective-date validity-window derivation when
`change_type` is `scd2_derived`.

For `scd1`, implementations materialise the current typed state without SCD2
history metadata. SCD1 may use `delete_detection.mode: field` to physically
remove matching records from the generated current-state output. Full target
rebuilds are controlled by `control_data.truncate_before_load`.

For `scd2_manual`, the source must provide SCD2 state values and the
specification must declare them in `target.fields` as ordinary fields:

- `valid_from_datetime`: timestamp value from which the row is valid.
- `valid_to_datetime`: timestamp value until which the row is valid.
- `is_current_flag`: `Y` when the row is current, otherwise `N`.
- `is_deleted_flag`: `Y` when the row represents a delete, otherwise `N`.

The `valid_from_datetime` and `valid_to_datetime` fields must use a timestamp
data type. The `is_current_flag` and `is_deleted_flag` fields must use `varchar(1)`.
Each of these fields must map from the matching uppercase physical source
column name: `VALID_FROM_DATETIME`, `VALID_TO_DATETIME`, `IS_CURRENT_FLAG`, or
`IS_DELETED_FLAG`.
These fields are typed, transformed, validated, and copied like any other target
field. Implementations must not derive SCD2 validity windows or state flags for
`scd2_manual`.

Implementations must reject a manual SCD2 load when the post-load candidate
state for any affected configured `business_key` would contain more than one row
with `is_current_flag = 'Y'`, a validity window where `valid_to_datetime` is not
after `valid_from_datetime`, or overlapping validity windows. For dbt
implementations, this validation must run against the live target relation plus
the incoming rows after applying the configured manual SCD2 append or upsert
semantics. Under `failure_mode: fail_load`, invalid candidate state must fail
the dbt build before the target table is changed. Under
`failure_mode: quarantine_row`, incoming rows for invalid candidate keys must be
written to quarantine rather than materialised.

For incremental dbt loads, `scd2_manual` uses `scd.update_mode` to control
whether incoming rows may replace existing target rows:

- `append_only`: append incoming rows without replacing existing target rows.
  This is the default when `update_mode` is omitted.
- `upsert`: replace target rows that have the same configured `business_key`
  plus `scd.update_key.fields` as an incoming source row, and insert incoming
  rows whose combined key does not already exist in the target.

`upsert` allows a later manual SCD2 source file to update the
`valid_to_datetime` and state flags for an existing version while adding a new
version. `business_key.fields` identifies the logical entity, while
`scd.update_key.fields` identifies the version row for upsert matching. If
`update_mode` is `upsert` and `update_key` is omitted, implementations should
default `update_key.fields` to `valid_from_datetime`. `scd.update_key` is valid
only when `update_mode` is `upsert`. `scd2_manual` must not declare SCD
parameters other than `update_mode` and `update_key`.

For `scd2_auto`, implementations preserve historical versions and generate the
following target metadata columns:

- `is_current_flag`: `Y` when the record is the current valid version, otherwise
  `N`. The physical data type is `varchar(1)`.
- `is_deleted_flag`: `Y` when the business entity has been logically deleted.
  The current specification does not define field-based delete detection for
  `scd2_auto`, so generated rows normally use `N`.
- `valid_from_datetime`: the timezone-aware timestamp from which the version is
  valid. The physical data type is `timestamp_tz`.
- `valid_to_datetime`: the timezone-aware timestamp until which the version is
  valid. The physical data type is `timestamp_tz`.

Generated SCD metadata columns for `scd2_auto` are not declared in
`target.fields`. Target field ids must not use generated SCD metadata column
names.

`delete_detection` values:

- `field`: remove rows where a target field equals the configured value. This
  mode is only valid for `scd1`.

`scd2_auto` has three SCD parameters:

- `insert_time`: scalar or templated timestamp value used as the proposed
  `valid_from_datetime` for incoming changes. It is typically a dbt variable.
- `scd2_auto_from_sot`: when `true`, the earliest version for a business key
  starts at the platform start-of-time timestamp. When `false`, the earliest
  version starts at `insert_time`. If omitted, implementations should default
  to `true`.
- `scd2_validation`: selects the dbt runtime validity-window validation mode.
  If omitted, implementations should default to `continuous`.

`scd2_validation` values:

- `sparse`: validity windows for the same generated business key may have gaps,
  but they must not overlap. Every record must have
  `valid_to_datetime > valid_from_datetime`.
- `continuous`: validity windows for the same generated business key must have
  no gaps or overlaps from the earliest `valid_from_datetime` for that key
  through the platform end-of-time timestamp. Every record must have
  `valid_to_datetime > valid_from_datetime`.

`scd2_auto` uses the generated business-key column declared by `business_key` to
join current target rows, partition validity windows, and identify affected
history for incremental updates. It also uses the generated
`business_data_hash` column described in [section 5.3](#53-business-data-hash)
to detect changed business values.

The reference platform start-of-time timestamp is `0001-01-01T00:00:00Z`. The
reference platform end-of-time timestamp is `9999-12-31T23:59:59Z`.

For `scd2_auto`, incoming changed rows use `insert_time` as their proposed
`valid_from_datetime`. When a generated row is the earliest known version for a
business key, `scd2_auto_from_sot` controls whether that row starts at the
platform start-of-time timestamp or at `insert_time`. Later versions start at
their own `insert_time`. SCD2 validity windows are inclusive: a row is valid
through `valid_to_datetime`. Generated `valid_to_datetime` values are therefore
derived as the timestamp tick immediately before the next later version starts
for the same generated business key; the latest version ends at the platform
end-of-time timestamp.

dbt implementations must validate generated `scd2_auto` windows before loading
them into the target. Under `sparse` validation, rows are invalid when
`valid_to_datetime` is not greater than `valid_from_datetime`, or when the next
row for the same generated business key starts at or before the current row's
`valid_to_datetime`. Under `continuous` validation, rows are also invalid when
the next row's `valid_from_datetime` is not the timestamp tick immediately after
the current row's `valid_to_datetime`, or when the latest row for the generated
business key does not end at or after `9999-12-30 00:00:00`. This
near-end-of-time threshold allows timezone-normalised end-of-time timestamps to
validate reliably. Invalid rows must fail the dbt load rather than being
materialised, leaving the existing target state unchanged.

SCD2 change detection compares the incoming `business_data_hash` with the
current target row for the same generated business key where
`is_current_flag = 'Y'`. When the hash differs, implementations must expire the
previous current version and create a new current version.

If an incoming `scd2_auto` record is inserted between existing historical
versions and its `business_data_hash` matches the adjacent previous or next
version, implementations must update the affected surrounding validity windows
rather than leaving adjacent duplicate business-data versions.

For `scd2_derived`, implementations preserve historical versions and generate
the same SCD2 target metadata columns as `scd2_auto`, but the proposed
`valid_from_datetime` comes from `scd.valid_from_datetime.source_column` or
`scd.valid_from_datetime.expression` rather than from load `insert_time`.

Generated SCD metadata columns for `scd2_derived` are not declared in
`target.fields`. Target field ids must not use generated SCD metadata column
names.

`scd2_derived` requires:

- `scd.valid_from_datetime.source_column` or
  `scd.valid_from_datetime.expression`: effective start value evaluated against
  the generated source rows.
- `business_key.fields`: target fields used to partition customer/entity
  history.
- `business_data_hash.fields`: source-derived target fields used to detect
  business-value changes for a version.

Prefer `source_column` when the source query already projects the business
effective start as a named column. Use `expression` when the effective start
needs inline SQL over generated source fields. `expression` remains supported
for backward compatibility.

`scd2_derived` supports:

- `derivation_scope: affected_keys`: recalculate only business keys affected by
  incoming changed rows.
- `validation_scope: affected_window`: validate only the post-load window being
  rewritten for the affected keys.
- `window_strategy: previous_and_next`: include the previous and next existing
  versions when recalculating an affected window.
- `valid_to_datetime.mode: next_valid_from`: derive each row end from the next
  later version for the same business key.
- `valid_to_datetime.offset`: subtract the configured timestamp tick, typically
  `{unit: nanosecond, value: -1}`.
- `current_flag.mode: latest_per_business_key`: mark the latest effective
  version as current.
- `deleted_flag.mode: fixed`: use a fixed deleted flag value, normally `N`.
- `deduplicate.order_by`: choose a deterministic winner when multiple incoming
  rows have the same business key and effective datetime.

When older source-system history such as V2 is loaded after newer V10 data for
the same business key, `scd2_derived` keeps V2 historical only when its derived
`valid_from_datetime` is older than the V10 version. If V2 and V10 need
independent current rows, `SOURCE_SYSTEM` must be part of `business_key.fields`.

The current specification does not define a source-current-flag override mode
for `scd2_derived`; generated `is_current_flag` is derived from the latest
effective version per business key.

Sample: SCD1 field delete detection.

```yaml
control_data:
  change_type: scd1
  scd:
    delete_detection:
      mode: field
      field: account_status
      value: DELETED
```

Sample: protected truncate rebuild.

```yaml
control_data:
  change_type: scd2_auto
  truncate_before_load: true
  business_key:
    fields:
      - account_id
  scd:
    insert_time: "{{ var('insert_time') }}"
```

Sample: SCD2 manual change handling.

```yaml
control_data:
  change_type: scd2_manual
```

Sample: SCD2 automatic change handling.

```yaml
control_data:
  change_type: scd2_auto
  business_key:
    fields:
      - account_id
  scd:
    insert_time: "{{ var('insert_time') }}"
    scd2_auto_from_sot: true
    scd2_validation: continuous
```

Sample: SCD2 derived change handling.

```yaml
control_data:
  change_type: scd2_derived
  business_key:
    fields:
      - customer_id
      - account_id
  business_data_hash:
    business_data_hash_mode: include
    fields:
      - customer_status_code
      - customer_name
      - email_address
  scd:
    derivation_scope: affected_keys
    validation_scope: affected_window
    window_strategy: previous_and_next
    valid_from_datetime:
      source_column: source_effective_from_datetime
    valid_to_datetime:
      mode: next_valid_from
      offset:
        unit: nanosecond
        value: -1
      end_of_time: "9999-12-31 23:59:59 +00:00"
    current_flag:
      mode: latest_per_business_key
    deleted_flag:
      mode: fixed
      value: "N"
```

Sample: SCD2 derived effective date with an inline expression.

```yaml
control_data:
  change_type: scd2_derived
  business_key:
    fields:
      - customer_id
  business_data_hash:
    business_data_hash_mode: include
    fields:
      - customer_status_code
  scd:
    valid_from_datetime:
      expression: coalesce(updated_datetime, created_datetime)
```

## 6. Source

```text
source ::= csv_source | table_source
source.format ::= csv | table
```

`source` describes the raw data being read before target field extraction,
typing, and validation.

`source.format` values:

- `csv`: delimited text input where fields may be selected by position, column
  name, or both.
- `table`: Snowflake table or query input where fields are selected by column
  name, or by native Snowflake path extraction from semi-structured columns.

### 6.1 CSV Source

```text
csv_source ::=
  format: csv
  header
  delimiter?
  quotechar?
  lineterminator?
  quoting?
  load_method?
  location?
  seed?
  upload?

load_method ::= stage | dbt_seed

location ::=
  database?
  schema?
  stage?
  filename?

seed ::=
  file
  name?
  database?
  schema?

upload ::=
  enabled: false
  | enabled: true
    file
    overwrite

header ::= true | false
delimiter ::= one character string
quotechar ::= one character string
lineterminator ::= string
quoting ::= minimal | all
database ::= templated_string
schema ::= templated_string
stage ::= templated_string
filename ::= templated_string
file ::= templated_string
name ::= identifier
overwrite ::= true | false
```

CSV dialect options align with Python CSV dialect concepts:

- `delimiter`: field separator character. Defaults to `,`.
- `quotechar`: field quoting character.
  Defaults to `"`.
- `lineterminator`: row terminator string. Defaults to `\r\n`.
- `quoting`: how the reader interprets quoted fields. Defaults to `minimal`.

`load_method` describes how dbt reads the CSV source:

- `stage`: dbt reads from a Snowflake stage using `location`. This is the
  default.
- `dbt_seed`: the implementation copies a local CSV file into the generated dbt
  project's `seeds` directory and generated models read from the loaded seed
  relation.

`location` describes where a CSV file is expected to be available for staged
dbt materialisation:

- `database`: database containing the stage. If omitted, the active dbt adapter
  and database session context resolves it.
- `schema`: schema containing the stage. Defaults to `AD_HOC`.
- `stage`: Snowflake stage name. Defaults to `@csv_stage`.
- `filename`: filename or stage path within the stage. No default.

When `location.stage` includes a leading `@`, implementations should not add a
second `@`.

`seed` describes how a CSV file is loaded using dbt's native seed mechanism:

- `file`: local CSV file copied into the generated dbt project's `seeds`
  directory. Variable expressions in `file` are resolved by Python tooling
  before dbt generation copies the file.
- `name`: dbt seed resource name. Defaults to `<target.id>__seed`.
- `database`: database for the seed relation. If omitted, the active dbt
  adapter and database session context resolves it.
- `schema`: schema for the seed relation. Defaults to the resolved staging
  schema.

For `load_method: dbt_seed`, `seed.file` is required. When `header` is `true`,
each non-fixed, non-macro target field must specify `field.source.column`. When
`header` is `false`, each non-fixed, non-macro target field must specify
`field.source.pos`; the implementation must synthesize a dbt seed header using
`COL_<N>` names, where `N` is the zero-based field position. CSV seed fields
may also use `source_macro`; when they do, `field.source.column` or
`field.source.pos` remains optional and may be supplied only as a helper source
column for the macro arguments. dbt seed loading is intended for small local
CSV files and development or reference-data workflows; stage-based loading
remains the preferred path for large operational source files.

`upload` describes whether the implementation should upload the local CSV file
before dbt generation:

- `enabled: false`: no upload is requested.
- `enabled: true`: Python tooling must upload `file` to `location` before dbt
  generation.
- `file`: local file path to upload.
- `overwrite`: whether an existing staged file should be replaced.

If `upload` is omitted, implementations should default to `enabled: false`.
CSV upload is handled by Python tooling outside dbt generation. dbt generation
assumes the file is already available in the configured stage.
`upload` is separate from `load_method: dbt_seed`; it is only used for
stage-based loading.

`quoting` values:

- `minimal`: parse quote characters only where needed for fields containing
  special characters.
- `all`: parse all fields as quoted fields.

Sample: CSV source.

```yaml
source:
  format: csv
  header: true
  load_method: stage
  location:
    schema: AD_HOC
    stage: "@csv_stage"
    filename: account.csv
  delimiter: ","
  quotechar: '"'
  lineterminator: "\r\n"
  quoting: minimal
```

Sample: CSV source loaded through dbt seed.

```yaml
source:
  format: csv
  header: true
  load_method: dbt_seed
  seed:
    file: ../csv/account_csv.csv
    name: account_seed
    schema: TMP
  delimiter: ","
  quotechar: '"'
  lineterminator: "\r\n"
  quoting: minimal
```

### 6.2 Table Source

```text
table_source ::=
  format: table
  (
    database?
    schema
    table
    query?
  | query
  )
  flatten?

flatten ::= flatten_source | flatten_source[]

flatten_source ::=
  column
  path?
  alias
  outer?
  mode?

database ::= string
schema ::= string
table ::= string
query ::= SQL SELECT statement
path ::= Snowflake semi-structured path
alias ::= identifier
outer ::= true | false
mode ::= object | array | both
```

Sample: table source.

```yaml
source:
  format: table
  query: |
    select *
    from raw.landing.account_file_landed
    where load_batch_id = '{{ var('load_batch_id') }}'
```

`query` is optional. When supplied, implementations must treat it as the full
SQL query that produces the source row set. The query must be a single
`select`/`with` query without a statement terminator. The generated
materialisation operates on the query result, so the query may filter by load
timestamp or batch identifier, select a subset of columns, join, deduplicate, or
perform other source shaping before target field mapping runs. If `query` is
omitted, `schema` and `table` are required and implementations should read all
rows from that source relation.

`flatten` optionally declares Snowflake-native row expansion over
semi-structured source data before field mapping. Implementations must compile
each entry to a `lateral flatten` operation. `column` names the source column or
an earlier flatten alias. `path` selects the array or object to expand using
Snowflake semi-structured path syntax. `alias` names the flatten output for
field mappings. `outer` maps to Snowflake `FLATTEN(..., OUTER => ...)` and
defaults to `false`.

`flatten.mode` values:

- `object`: expand object entries only.
- `array`: expand array entries only.
- `both`: expand object and array entries. This is the default.

Sample: table source with Snowflake flattening.

```yaml
source:
  format: table
  schema: raw
  table: landed_order_events
  flatten:
    column: payload
    path: customer.orders
    alias: order_item
    mode: array
```

## 7. Target

```text
target ::=
  id
  database?
  schema
  table_name?
  tags?
  fields[]

id ::= identifier
database ::= string
schema ::= string
table_name ::= string
tags ::= map<string, scalar>
```

`target` describes the typed data produced by the materialisation.

`target.id` is the stable identifier for the target. It is compared
case-insensitively like other identifiers.

`target.schema` is required for complete or resolved specifications. If
`target.database` is omitted, the active dbt adapter and database session
context resolves the target database.

`target.table_name` is the target table name. If omitted, implementations
should use `target.id` as the table name.

`target.tags` is optional metadata for target-level classification, governance,
or platform policy integration. dbt implementations that support Snowflake tags
should apply these as table tags after the target relation exists. Tags do not
control generated surrogate keys, generated business keys, or
`business_data_hash`; those columns are controlled by `control_data`.

`target.fields` must contain at least one field.

### 7.1 Target Audit Metadata

Every target row created or changed by a materialisation process must include
operational traceability metadata. These audit metadata columns are generated by
the implementation and are not declared in `target.fields`.

The target audit metadata columns are:

- `audit_data_process_key`: the operational process key for the pipeline
  execution or run that produced the row. This links target rows to centralized
  operational metadata for lineage tracing, reconciliation, and observability.
  The physical data type is `varchar(64)`.
- `audit_created_datetime`: the timezone-aware timestamp when the row was first created in the
  platform. This value is immutable for the lifetime of the row and supports
  data freshness checks and initial load tracking. The physical data type is
  `timestamp_tz`.
- `audit_last_changed_datetime`: the timezone-aware timestamp of the most recent
  change applied to the row. This value is updated on every insert, update, or
  delete and supports incremental processing and observability. The physical
  data type is `timestamp_tz`.

The generated surrogate-key, business-key, audit, and `scd2_auto` metadata
column data type contract is:

| Column | Physical data type | Value domain |
| --- | --- | --- |
| `<target.id>_KEY` | `varchar(36)` | generated GUID value |
| `<target.id>_BUSINESS_KEY` | `varchar` | SHA2-256 hashed business key value |
| `is_current_flag` | `varchar(1)` | `Y` or `N` |
| `is_deleted_flag` | `varchar(1)` | `Y` or `N` |
| `valid_from_datetime` | `timestamp_tz` | timezone-aware timestamp value |
| `valid_to_datetime` | `timestamp_tz` | timezone-aware timestamp value |
| `business_data_hash` | `varchar(64)` | hash value |
| `audit_created_datetime` | `timestamp_tz` | timezone-aware timestamp value |
| `audit_last_changed_datetime` | `timestamp_tz` | timezone-aware timestamp value |
| `audit_data_process_key` | `varchar(64)` | operational process key |

Target field ids must not use the reserved generated metadata column names,
except that `scd2_manual` specifications must declare `valid_from_datetime`,
`valid_to_datetime`, `is_current_flag`, and `is_deleted_flag` as ordinary target
fields.

For SCD2 targets, implementations must order the final physical output columns
by logical group rather than by the raw declaration order in an abstract parent
specification. The abstract spec order is primarily for shared configuration
and readability, not for controlling the final Snowflake table layout.

Implementations should emit SCD2 target columns in this order:

1. generated surrogate key, when enabled
2. generated business key, when enabled
3. declared business fields in their declared order
4. SCD2 state fields in this order: `IS_CURRENT_FLAG`, `IS_DELETED_FLAG`,
   `VALID_FROM_DATETIME`, `VALID_TO_DATETIME`
5. generated `BUSINESS_DATA_HASH`, when enabled
6. declared source audit fields, for example `AUDIT_CREATED_SOURCE` and
   `AUDIT_LAST_CHANGED_SOURCE`, in their declared order
7. generated system audit fields in this order: `AUDIT_CREATED_DATETIME`,
   `AUDIT_LAST_CHANGED_DATETIME`, `AUDIT_DATA_PROCESS_KEY`

This lets a concrete reference-data spec inherit shared SCD2 behavior from an
abstract parent while still producing a stable physical column layout such as
key columns first, source business columns next, SCD2 metadata after that, and
audit metadata at the end.

Sample: target table definition.

```yaml
target:
  id: account
  database: analytics
  schema: business
  tags:
    DATA_CLASSIFICATION: CONFIDENTIAL
  fields:
    - id: account_id
      source:
        column: account_id
      data_type: varchar(20)
      nullable: false
```

## 8. Target Fields

```text
field ::=
  id
  source
  data_type
  transforms?
  nullable?
  tags?
  unique?
  validations?

id ::= identifier
nullable ::= true | false
tags ::= map<string, scalar>
unique ::= true | false
```

`target.fields` is an ordered list of fields to materialise in the target.
Each field `id` is an identifier and must be unique within the resolved target
field list. Field id comparison is case-free, so `account_id` and `ACCOUNT_ID`
are the same field id.

If `unique` is `true`, the loaded target values for that field must be unique
within the materialised source set. Null values are not considered duplicates.
Uniqueness is an automatically applied load-time validation for that field and
is handled according to `failure_mode`.

`field.tags` is optional metadata for column-level classification, governance,
or platform policy integration. dbt implementations that support Snowflake tags
should apply these as column tags after the target relation exists.

Sample: target field.

```yaml
fields:
  - id: account_id
    source:
      pos: 0
      column: account_id
    data_type: varchar(20)
    transforms:
      - type: trim
    nullable: false
    tags:
      DATA_CLASSIFICATION: PII
      DATA_CATEGORY: IDENTIFIER
    unique: true
```

## 9. Field Source

```text
field.source ::= csv_field_source | table_field_source
field.lookup ::= table_lookup_source

csv_field_source ::=
  pos?
  column?
  default_value?
  | source_macro
  | fixed_value

table_field_source ::=
  column
  snowflake_path?
  default_value?
  default_from_field?
  | default_value
  | default_from_field
  | source_macro
  | fixed_value

pos ::= integer >= 0
column ::= string
snowflake_path ::= Snowflake semi-structured path
default_value ::= scalar
default_from_field ::= target field id
source_macro ::= macro + args?
fixed_value ::= scalar
table_lookup_source ::=
  reference_entity
  reference_attribute
  source_expression
  required?

reference_entity ::= database.schema.table
reference_attribute ::= reference field id
source_expression ::= SQL expression evaluated against source_query
required ::= boolean
```

For CSV sources, a field may be selected by position, column name, or both. If
both are supplied, a mismatch between the header at `pos` and `column` is an
error.

When `source.header` is `false`, the CSV has no physical column names. At load
time, implementations must infer source column names from position using
`COL_<N>`, where `N` is the zero-based `field.source.pos`. For example, the
first field is `COL_0` and the third field is `COL_2`. Stage-backed
implementations should alias positional stage columns to these names; dbt-seed
implementations should synthesize a seed header with these names.

For table sources, a field must be selected by column name. `pos` is invalid for
table sources. `snowflake_path` may be supplied to extract a scalar value from a
Snowflake `VARIANT`, `OBJECT`, or `ARRAY` column, or from the `value` column of
a configured flatten alias. The extracted value enters the TMS transform and
validation pipeline as a `varchar` source value; target typing still happens in
the normal TMS field pipeline.

For any source format, `fixed_value` supplies the same scalar source value for
every generated source row. It is mutually exclusive with `pos`, `column`, and
`snowflake_path`. The fixed value enters the TMS transform and validation
pipeline as a `varchar` source value; target typing still happens in the normal
TMS field pipeline.

For any source format, `default_value` supplies a fallback scalar source value
when the selected source value is null or an empty string. For table sources, it
may also be used on its own to generate a literal source value without requiring
a dummy source query column. It may be combined with `pos`, `column`, or
`snowflake_path`, and enters the TMS transform and validation pipeline as a
`varchar` source value before target typing. It is mutually exclusive with
`fixed_value`.

For table sources, `default_from_field` supplies a source value from another
target field. It may be used on its own, or combined with `column`,
`snowflake_path`, and `default_value`. When combined with `column`, TMS uses the
selected source value first, the referenced field second, and the scalar default
last. It is mutually exclusive with `fixed_value`.

For table sources, `source.macro` supplies the field source expression from a
custom Python macro object. `source.args` is an optional mapping of scalar values
passed to the generated dbt macro. This keeps TMS generic while allowing
consumer projects to provide domain-specific SQL expressions such as reference
key lookups.

For table sources, `lookup` supplies a target field value from a reference
table. `reference_entity` names the reference table, `reference_attribute` names
the reference column matched by `source_expression`, and TMS generates a
current/non-deleted reference join. `lookup` is mutually exclusive with
`source`.

TMS renders `reference_entity` as declared. If the physical database is
environment-prefixed, include that Jinja directly in the spec. For example,
`{{ var("ENV_PREFIX", "") }}REFERENCE.CORE.CUSTOMER_STATUS`.

Any `field.source.column` value specified without `snowflake_path` in
`target.fields` must be unique within the resolved target field list. Column
comparison is case-free, so `account_id` and `ACCOUNT_ID` are the same source
column name. Multiple fields may use the same semi-structured source column when
each field supplies its own `snowflake_path`.

Sample: CSV field source using both position and column.

```yaml
fields:
  - id: account_id
    source:
      pos: 0
      column: account_id
    data_type: varchar(20)
```

Sample: table field source using column only.

```yaml
fields:
  - id: account_id
    source:
      column: account_id
    data_type: varchar(20)
```

Sample: table field source using Snowflake path extraction.

```yaml
fields:
  - id: opened_date
    source:
      column: payload
      snowflake_path: account.openedDate
    data_type: date
```

Sample: table field source using a flatten alias.

```yaml
fields:
  - id: order_id
    source:
      column: order_item
      snowflake_path: id
    data_type: varchar(20)
```

Sample: field source using a fixed value.

```yaml
fields:
  - id: is_current_flag
    source:
      fixed_value: "Y"
    data_type: varchar(1)
```

Sample: field source using a default value fallback.

```yaml
fields:
  - id: country_code
    source:
      column: country_code
      default_value: "UNKNOWN"
    data_type: varchar(20)
```

Sample: table field using a reference lookup.

```yaml
fields:
  - id: customer_status_key
    data_type: varchar(64)
    nullable: false
    source:
      macro: reference_macros.reference_lookup
      args:
        reference_type: CUSTOMER_STATUS
        source_system: V10
        source_code_expression: |
          case
            when AMNA_ADD_STATUS != 99 then 'PROF'
            when AMNA_STATUS = 0 then 'ENAB'
            when AMNA_STATUS = 1 then 'DISA'
            when AMNA_STATUS = 2 then 'DELE'
            else 'DQMapping'
          end
```

## 10. Data Types

```text
data_type ::= string
```

The initial data type format should align with SQL-like type declarations.

Sample: SQL-like data type declarations.

```yaml
fields:
  - id: account_id
    source:
      column: account_id
    data_type: varchar(20)
  - id: balance
    source:
      column: balance
    data_type: decimal(18,2)
```

Implementations should parse and validate supported data types rather than
treating them as arbitrary strings. See also
[section 12](#12-validation-rules) for automatic data type validation.

## 11. Field Transforms

```text
transforms ::= transform_rule[]

transform_rule ::= trim | parse_date | parse_timestamp | round | custom

trim ::=
  type: trim
  side?

side ::= both | left | right

parse_date ::=
  type: parse_date
  format

parse_timestamp ::=
  type: parse_timestamp
  format
  timezone_if_missing?
  time_if_missing?

format ::= Python datetime format string
timezone_if_missing ::= Z | UTC | local
time_if_missing ::= start_of_day | end_of_day

round ::=
  type: round
  scale
  mode?

scale ::= integer >= 0
mode ::= half_up | half_even | down | up

custom ::=
  type: custom
  macro

macro ::= python_macro_name
python_macro_name ::= dotted Python reference
```

Transforms are simple single-column instructions applied to the extracted field
value. They must not reference other source fields or target fields.

Transforms are applied in the order they appear. A transform output must be
compatible with the field's `data_type`.

`transform_rule.type` values:

- `trim`: remove whitespace from a string value.
- `parse_date`: parse a string value into a date using `format`.
- `parse_timestamp`: parse a string value into a timezone-aware timestamp using
  `format`. If the parsed value does not include timezone information, the
  value must be rejected unless `timezone_if_missing` is supplied. If the
  source format does not include a time component, `time_if_missing` may be
  supplied to set the parsed timestamp to the start or end of the parsed day.
- `round`: round a numeric value to a decimal `scale`.
- `custom`: apply a named Python macro to the column.

`side` values:

- `both`: trim leading and trailing whitespace.
- `left`: trim leading whitespace.
- `right`: trim trailing whitespace.

If `side` is omitted, implementations should default to `both`.

`mode` values:

- `half_up`: round half values away from zero.
- `half_even`: round half values to the nearest even digit.
- `down`: round toward zero.
- `up`: round away from zero.

If `mode` is omitted, implementations should default to `half_up`.

`timezone_if_missing` values:

- `Z`: treat a timestamp without timezone information as UTC with a `Z`
  timezone designator.
- `UTC`: treat a timestamp without timezone information as UTC.
- `local`: treat a timestamp without timezone information as local to the
  runtime environment.

`time_if_missing` values:

- `start_of_day`: when the timestamp format has no time component, set the
  parsed time to `00:00:00`.
- `end_of_day`: when the timestamp format has no time component, set the parsed
  time to the last representable instant of that day.

Date and timestamp `format` values use Python `datetime` `strptime` /
`strftime`-style format codes. Timestamp formats must include a timezone offset
or timezone-bearing value unless `timezone_if_missing` is supplied.
Implementations must reject parsed timestamps that do not include timezone
information unless an explicit missing-timezone policy is configured. dbt
implementations should map these format strings to database-native parsing
functions where required.

Common date and timestamp format examples:

- `%Y-%m-%d`: `2026-08-13`
- `%d/%m/%Y`: `13/08/2026`
- `%Y-%m-%d %H:%M:%S` with `timezone_if_missing: UTC`: `2026-08-13 14:30:00`
- `%d/%m/%Y` with `timezone_if_missing: UTC` and
  `time_if_missing: end_of_day`: `13/08/2026`
- `%Y-%m-%dT%H:%M:%S%z`: `2026-08-13T14:30:00+1000`
- `%Y-%m-%dT%H:%M:%S%z`: `2026-08-13T14:30:00Z`

Custom transforms are a constrained Python extension point. `macro` names a
Python macro object using dotted module syntax, for example
`account_macros.normalise_account_id`. The macro is applied to the current
column only.

```text
custom_transform_macro ::= macro(value, field?, rule?)
custom_transform_macro_result ::= transformed value
```

dbt implementations call Python macros at generation time. A custom macro module
must expose a SQL-generation hook that emits dbt/Jinja SQL macros or SQL
expressions for the transient dbt project. A custom macro may also expose a
same-named Python callable for local execution by tools such as
`tms validate`. The Python callable itself is not executed inside dbt against
database row values.

Implementations must not depend on dbt Jinja importing or calling
arbitrary project Python modules from a dbt macro. The portable dbt path is to
call Python macros before DBT-compilation time, then generate dbt/Jinja SQL
artifacts that dbt can compile and the target database can execute. A future
extension may allow a Python macro to generate database-native Python UDFs or
dbt Python models, but that is a separate execution mode from SQL model macro
generation.

Python macro object contract:

```text
python_macro_reference ::= module_path "." macro_object_name
module_path ::= Python module path resolved from runtime macro_paths[]
macro_object_name ::= generated dbt macro name
```

The referenced object must implement the Type Materialisation macro interface.

```text
class TypeMaterialisationMacro:
  supports_python_execution: boolean = false
  generate_dbt_macro() -> dbt_macro_sql
  execute(value, field?, rule?) -> transformed_value  # optional

dbt_macro_sql ::= complete dbt/Jinja macro definition
```

`generate_dbt_macro` is required. It must return a dbt/Jinja macro definition
whose macro name is exactly `macro_object_name`. The generated transform dbt
macro must accept `column_expression` and return a SQL expression for the
transformed value.

For local validation support, a custom transform may expose a Python callable
through `execute` and set `supports_python_execution` to `true`.

```text
transform_callable ::= execute(value, field?, rule?)
transform_callable_result ::= transformed value
```

If the Python callable is absent, `tms validate` should warn that the custom
transform could not be executed locally and should continue using the
untransformed value for local checks.

Sample: common field transforms.

```yaml
fields:
  - id: account_name
    source:
      column: account_name
    data_type: varchar(255)
    transforms:
      - type: trim
  - id: opened_on
    source:
      column: opened_on
    data_type: date
    transforms:
      - type: parse_date
        format: "%d/%m/%Y"
  - id: balance
    source:
      column: balance
    data_type: decimal(18,2)
    transforms:
      - type: round
        scale: 2
        mode: half_up
```

Sample: custom Python macro transform.

```yaml
fields:
  - id: account_id
    source:
      column: account_id
    data_type: varchar(20)
    transforms:
      - type: custom
        macro: account_macros.normalise_account_id
```

## 12. Validation Rules

```text
validations ::= validation_rule[]

validation_rule ::=
  min_length
  | max_length
  | regex
  | allowed_values
  | min_value
  | max_value
  | precision
  | custom_validation

min_length ::=
  type: min_length
  value

max_length ::=
  type: max_length
  value

regex ::=
  type: regex
  pattern

allowed_values ::=
  type: allowed_values
  values[]

min_value ::=
  type: min_value
  value

max_value ::=
  type: max_value
  value

precision ::=
  type: precision
  precision
  scale?

custom_validation ::=
  type: custom
  macro

value ::= scalar
pattern ::= regular expression string
precision ::= integer >= 1
scale ::= integer >= 0
macro ::= python_macro_name
python_macro_name ::= dotted Python reference
```

`validation_rule.type` values:

- `min_length`: string length must be greater than or equal to `value`.
- `max_length`: string length must be less than or equal to `value`.
- `regex`: string value must match `pattern`.
- `allowed_values`: value must be one of `values`; `values` must contain at
  least one scalar value.
- `min_value`: value must be greater than or equal to `value`.
- `max_value`: value must be less than or equal to `value`.
- `precision`: numeric value must fit the configured decimal precision and
  optional scale.
- `custom`: apply a named custom validation to the column.

Every target field has an automatically applied validation that the transformed
source value can be represented by the field's declared `data_type`. This
validation applies even when `validations` is omitted. A value that cannot be
converted to the declared `data_type`, or that cannot be represented within that
type's constraints, is a validation or conversion failure and is handled
according to `failure_mode`.

Custom validation macro signature:

```text
custom_validation_macro ::= macro(value, row?, field?, rule?)
custom_validation_macro_result ::= null | true | false | string
```

A custom validation returns no failure when it returns `NULL` or `true`. It
returns a generic failure when it returns `false`, and returns failure details
when it returns a string. The string should explain the first failure detected
for the current column value.

dbt implementations call Python validation macros at generation time to emit
dbt/Jinja SQL validation logic for the transient dbt project. A custom
validation macro module must expose a SQL-generation hook. Local validation
tooling may call an optional same-named Python callable directly against parsed
input values.

Implementations must not depend on dbt Jinja importing or calling
arbitrary project Python modules from a dbt macro. For SQL-model
materialisation, the Python validation macro is called by the implementation
before DBT-compilation time and must produce, or participate in
producing, SQL/Jinja artifacts that can be compiled by dbt and executed by the
target database.

Python validation macro object contract:

```text
python_macro_reference ::= module_path "." macro_object_name
module_path ::= Python module path resolved from runtime macro_paths[]
macro_object_name ::= generated dbt macro name
```

The referenced object must implement the Type Materialisation macro interface.

```text
class TypeMaterialisationMacro:
  supports_python_execution: boolean = false
  generate_dbt_macro() -> dbt_macro_sql
  execute(value, row?, field?, rule?) -> null | true | false | string  # optional

dbt_macro_sql ::= complete dbt/Jinja macro definition
```

`generate_dbt_macro` is required. It must return a dbt/Jinja macro definition
whose macro name is exactly `macro_object_name`. The generated validation dbt
macro must accept `column_expression` and return a nullable SQL string
expression. The SQL expression returns `NULL` when validation passes, and a
non-null failure message when validation fails.

For local validation, a custom validation may expose a Python callable with the
`execute` method and set `supports_python_execution` to `true`. The callable
accepts the current value and may optionally accept context keyword arguments.

```text
validation_callable ::= execute(value, row?, field?, rule?)
validation_callable_result ::= null | true | false | string
```

If the Python callable is absent, `tms validate` should warn that the custom
validation could not be executed locally and should continue with the other
local checks.

Sample: common field validation rules.

```yaml
validations:
  - type: min_length
    value: 16
  - type: max_length
    value: 20
  - type: regex
    pattern: "^[A-Z0-9]+$"
  - type: allowed_values
    values:
      - ACTIVE
      - CLOSED
  - type: precision
    precision: 18
    scale: 2
```

Sample: custom validation rule.

```yaml
validations:
  - type: custom
    macro: account_macros.validate_account_number
```

The JSON Schema validates the shape of validation rules, not the runtime
behavior of each validator.

## 13. Variable Usage

String values may contain a constrained subset of dbt-compatible Jinja variable
expressions.

```text
templated_string ::= string with zero or more variable expressions

variable_expression ::=
  "{{ var('name') }}"
  | "{{ var('name', 'default') }}"
  | "{{ env_var('name') }}"
  | "{{ env_var('name', 'default') }}"
  | "{{ tms_var('name') }}"
  | "{{ tms_var('name', 'default') }}"
```

`tms_var` expressions are resolved by TMS from command-line `--vars` after YAML
parsing and before schema validation, CSV validation, or dbt project
generation. If a `tms_var` expression provides a default, the default is used
when the variable is omitted from `--vars`.

`var` and `env_var` expressions are dbt-time expressions. They may remain in
generated dbt files and be resolved by dbt at runtime.

Sample: table source with dbt-style variables.

```yaml
source:
  format: table
  query: |
    select *
    from {{ env_var('RAW_DATABASE') }}.{{ var('raw_schema', 'landing') }}.{{ env_var('ENV') }}_account_file_landed
    where load_batch_id = '{{ var('load_batch_id') }}'
```

## 14. Validation Phases

```text
SCHEMA time ::= YAML parses and conforms to JSON Schema
PARSE time ::= rules that require the parsed YAML document
INHERITANCE time ::= parent specifications are loaded and overlaid
RESOLVE time ::= rules that require variable resolution
DBT_COMPILATION time ::= dbt artifacts are generated and compiled
LOAD time ::= rules that require source data or source metadata
```

Schema-time rules for concrete specifications are enforced by
`schema/type-materialisation.schema.json`. Schema-time shape rules for abstract
specifications are enforced by
`schema/type-materialisation-abstract.schema.json`.

Failures before LOAD time are recorded against the `JOB_START` event when a job
table row can be emitted. Schema-time and parse-time failures use
`FAILED_TO_PARSE`, inheritance-time failures use `FAILED_TO_INHERIT`,
resolve-time failures use `FAILED_TO_RESOLVE`, and DBT-compilation-time
failures use `FAILED_TO_COMPILE`.

Parse-time rules:

- Complete concrete specifications must declare `control_data.change_type`.
- `source.format ::= csv | table`.
- For `source.format = csv`, each field source must specify at least one of
  `pos`, `column`, `macro`, or `fixed_value`.
- For `source.format = csv` and `source.header = false`, `field.source.column`
  is invalid.
- For `source.format = csv`, `source.load_method = dbt_seed`, and
  `source.header = true`, each non-fixed, non-macro field source must specify
  `column`.
- For `source.format = csv`, `source.load_method = dbt_seed`, and
  `source.header = false`, each non-fixed, non-macro field source must specify
  `pos`.
- For `source.format = table`, each field must specify `source` or `lookup`.
- For `source.format = table`, each `field.source` must specify `column`,
  `default_value`, `default_from_field`, `macro`, or `fixed_value`.
- For `source.format = table`, `field.lookup` is mutually exclusive with
  `field.source`.
- For `source.format = table`, `field.source.pos` is invalid.
- For `source.format = table`, `field.source.snowflake_path` is valid only with
  `field.source.column`.
- `field.source.fixed_value` is mutually exclusive with `field.source.pos`,
  `field.source.column`, and `field.source.snowflake_path`.
- `field.source.default_value` is mutually exclusive with
  `field.source.fixed_value`.
- `field.source.default_from_field` is supported only for table sources, must
  reference another target field, and is mutually exclusive with
  `field.source.fixed_value`.
- For `source.format = table`, each `source.flatten.alias` must be unique and
  must not duplicate a physical source column used without `snowflake_path`.
- For `source.format = table`, if `source.query` is supplied, it must be a
  single SQL query starting with `select` or `with` and must not include a
  statement terminator.
- Target field ids must be unique within the resolved target field list.
- Specified `field.source.column` values without `snowflake_path` must be unique
  within the resolved target field list.
- Target field ids must not use reserved audit metadata column names:
  `audit_data_process_key`, `audit_created_datetime`, or
  `audit_last_changed_datetime`.
- Target field ids must not use generated `scd2_auto` metadata column names:
  `is_current_flag`, `is_deleted_flag`, `valid_from_datetime`,
  `valid_to_datetime`, or `business_data_hash`. `scd2_manual` specifications
  must declare `valid_from_datetime`, `valid_to_datetime`, `is_current_flag`,
  and `is_deleted_flag` as target fields.
- `control_data.business_key` is required for complete specifications unless
  `skip_business_key` is `true`. `scd2_auto` specifications must declare
  `business_key` even when `skip_business_key` is `true`.
- `business_key.fields` must contain at least one target field id, and each
  configured field id must exist in `target.fields`.
- When surrogate-key generation is enabled, `business_key.fields` must not
  include the generated surrogate-key column name.
- If `skip_business_key` is omitted or `false`, the generated business-key
  column name `<target.id>_BUSINESS_KEY` must not collide with a target field id
  or generated metadata field id.
- If `skip_surrogate_key` is omitted or `false`, the generated surrogate-key
  column name `<target.id>_KEY` must not collide with a target field id, the
  generated business-key column name, or another generated metadata field id.
- If `business_data_hash.business_data_hash_mode = include`, `fields` must
  contain at least one field id.
- `business_data_hash.fields` entries must exist in `target.fields`.
- If `business_data_hash.business_data_hash_mode = include`,
  `business_data_hash.fields` must not reference generated surrogate-key
  columns, generated business-key columns, `scd2_auto` metadata columns,
  `scd2_manual` validity/state fields, or `scd.update_key.fields`.
- For `delete_detection.mode = field`, `field` and `value` are required, and
  `field` must reference a target field id. This mode is valid only when
  `change_type = scd1`.
- If `truncate_before_load` is `true`, dbt implementations must require
  `allow_truncate: true` before executing the rebuild as a safety precaution
  against accidental destructive target replacement.
- For `change_type = scd2_manual`, `scd` may contain `update_mode` and
  `update_key`, and must not contain other SCD parameters.
- For `change_type = scd2_manual`, `scd.update_key` is valid only when
  `scd.update_mode = upsert`.
- For `change_type = scd2_manual`, every `scd.update_key.fields` value must
  reference a target field id. When `scd.update_mode = upsert` and
  `scd.update_key` is omitted, implementations should default
  `scd.update_key.fields` to `valid_from_datetime`.
- For `change_type = scd2_manual`, target fields must include
  `valid_from_datetime`, `valid_to_datetime`, `is_current_flag`, and `is_deleted_flag`.
- For `change_type = scd2_manual`, `valid_from_datetime` and
  `valid_to_datetime` must use timestamp data types, while `is_current_flag` and
  `is_deleted_flag` must use `varchar(1)`.
- For `change_type = scd2_manual`, `field.source.column` for those SCD2 fields
  must be `VALID_FROM_DATETIME`, `VALID_TO_DATETIME`, `IS_CURRENT_FLAG`, and
  `IS_DELETED_FLAG`, respectively.
- For `change_type = scd1`, `insert_time`, `scd2_auto_from_sot`, and
  `scd2_validation` must be omitted.
- For `change_type = scd2_auto`, `scd.insert_time` is required and
  `delete_detection.mode = field` is invalid.
- For `change_type = scd2_auto`, `scd2_validation` defaults to `continuous`
  when omitted.

Inheritance-time rules:

- Parent specifications referenced by `extends` must be available before
  variable expressions are resolved.
- An abstract specification may remain incomplete while it is only used as an
  inheritance parent.
- A specification selected for materialisation must be complete after
  inheritance resolution.

Resolve-time rules:

- Supported variable expressions must resolve to strings.
- A variable with no value and no default fails resolution.
- Unsupported Jinja expressions are invalid.
- Custom Python transform macros must be available to implementations before the
  materialisation is executed.

DBT-compilation-time rules:

- Resolved specifications must generate valid dbt artifacts.
- Generated dbt artifacts must compile successfully before source data is
  loaded or target data is changed.
- dbt artifacts generated from custom Python transform and validation macros
  must be available in the generated dbt project before compilation.

Load-time rules:

- For `source.format = csv`, if `field.source.pos` and `field.source.column`
  are both specified, the source header at `pos` must equal `column`.
- For `source.format = table`, `field.source.column` must exist in the source
  table.
- For `source.format = table`, if `field.source.snowflake_path` is specified and
  `field.source.column` is not a configured flatten alias, the source column
  must be a Snowflake `VARIANT`, `OBJECT`, or `ARRAY` column. If the
  implementation can inspect table metadata before materialisation, it must fail
  before target data is changed when this condition is not met. Missing path
  values produce null source values and are handled by ordinary TMS field
  validation.
- For any field with `unique: true`, non-null materialised values for that field
  must be unique within the loaded source set.

## 15. Inheritance

```text
extends ::= parent_specification_id
parent_specification_id ::= identifier

parent_specification_path ::= specification_search_path + "/" + parent_specification_id + (".yaml" | ".yml")
specification_search_path ::= same_directory | runtime inheritance path
inheritance_chain ::= oldest_ancestor -> ... -> parent -> child
resolved_specification ::= overlay(inheritance_chain)

source_override ::= partial source mapping
target_override ::= partial target mapping
field_override ::= id + partial field mapping
```

Inheritance allows a specification to reuse a shared baseline. It is a simple
additive overlay model: parent attributes are loaded first, child attributes are
loaded over the top, and the child always wins when both define the same
attribute.

Abstract specifications are intended for inheritance. They may omit any element
that would be required in a complete specification, including `source`, target
id, target schema, target fields, field source, or field data type. Omitted
attributes must be supplied by descendants before the
specification can be materialised. Supplied attributes in an abstract
specification must still satisfy the same schema-time and parse-time rules as
the equivalent attributes in a complete specification.

Sample: child specification extending a parent.

```yaml
id: customer_reference_code_data
extends: abstract_reference_code_data
description: Customer reference code data.
```

Sample: parent specification file named `abstract_reference_code_data.yaml`.

```yaml
id: abstract_reference_code_data
description: Shared shape for reference code data tables.
control_data:
  materialisation_type: table
  change_type: scd1
  failure_mode: quarantine_row
source:
  format: table
  database: raw
  schema: reference_data
  table: abstract_reference_code_data
target:
  id: abstract_reference_code_data
  database: analytics
  schema: business
  fields:
    - id: code
      source:
        column: code
      data_type: varchar(50)
      nullable: false
      unique: true
```

Sample: child specification overriding parent attributes and adding a field.

```yaml
id: customer_reference_code_data
extends: abstract_reference_code_data
description: Customer reference code data.
source:
  table: customer_reference_code_data
target:
  id: customer_reference_code_data
  fields:
    - id: code
      data_type: varchar(20)
    - id: description
      source:
        column: description
      data_type: varchar(255)
      nullable: true
```

Inheritance rules:

- A child specification references one parent through `extends`.
- Multiple layers of inheritance are supported through chained `extends`.
- Parent specifications are resolved from the child specification directory
  first, then from any runtime-provided inheritance paths. The path values are
  supplied by the implementation runtime environment, not by the YAML
  specification.
- Parent specification files are expected to be named `<id>.yaml` or `<id>.yml`.
- If a parent id resolves to multiple candidate files within the same searched
  directory, the implementation must fail inheritance resolution rather than
  silently choosing one.
- Implementations load the oldest ancestor first, then each descendant in order,
  ending with the child specification.
- Child scalar attributes override parent scalar attributes.
- Child mapping attributes are merged over parent mapping attributes.
- Child target field definitions are matched to parent target field definitions
  by `id`.
- A child target field may override inherited field properties.
- A child may add target fields not present in the parent.
- A child cannot remove a parent attribute. It can only inherit it, override it,
  or add additional attributes.
- Cyclic inheritance is invalid.
- The final resolved specification must satisfy the complete specification
  shape.

### 15.1 Inheritance Examples

Sample: parent adds the base field list.

```yaml
target:
  fields:
    - id: code
      source:
        column: code
      data_type: varchar(50)
      nullable: false
```

Sample: child adds a new field.

```yaml
target:
  fields:
    - id: description
      source:
        column: description
      data_type: varchar(255)
      nullable: true
```

Sample: final result after list items are merged.

```yaml
target:
  fields:
    - id: code
      source:
        column: code
      data_type: varchar(50)
      nullable: false
    - id: description
      source:
        column: description
      data_type: varchar(255)
      nullable: true
```

Sample: parent field to be overridden.

```yaml
target:
  fields:
    - id: code
      source:
        column: code
      data_type: varchar(50)
      nullable: false
      unique: true
```

Sample: child field with the same `id`.

```yaml
target:
  fields:
    - id: code
      data_type: varchar(20)
```

Sample: final result after the matching field is overlaid.

```yaml
target:
  fields:
    - id: code
      source:
        column: code
      data_type: varchar(20)
      nullable: false
      unique: true
```

In the second example, the child overrides `data_type` because it supplied a new
value for the same field `id`. The inherited `source`, `nullable`, and `unique`
attributes remain because child specifications cannot remove inherited
attributes.

## 16. Implementation

The implementation should produce dbt artifacts that perform materialisation,
transforms, validations, quarantine handling, and target writes.

Python may be used for repository tooling such as YAML parsing, JSON Schema
validation, inheritance resolution, dbt artifact generation, golden-file tests,
and test harnesses. Python is not a separate materialisation runtime for this
specification.

Implementations may generate a transient dbt project at runtime from resolved
specifications. Generated dbt artifacts are an execution detail and are
not source-of-truth specification files.

If a runtime allows the generated dbt project directory to be supplied
explicitly, generation must fail when that directory already exists and contains
any files or directories. Implementations may create a missing directory, or use
an already existing empty directory, but must not merge newly generated artifacts
with previous generated content.

The transient dbt project should use a predictable runtime structure:

```text
runtime_root/
  python_macros/
    custom/
      *.py
  dbt_project.yml
  models/
    generated/
      <target.id>.sql
  macros/
    reference/
      *.sql
    generated/
      *.sql
  target/
  logs/
```

`models/generated/` contains generated dbt model files for resolved concrete
specifications.

The implementation should build the transient dbt project structure, compile
the generated project, and then run the compiled dbt project. Compilation must
complete successfully before any source data is loaded or target data is
changed.

`python_macros/custom/` contains user-supplied Python macro modules required by
custom transform or validation rules. Custom Python macro files are supplied
through runtime macro paths, not through YAML specification properties.

`macros/reference/` contains implementation-supplied dbt/Jinja macros required
to materialise the specification.

`macros/generated/` contains dbt/Jinja macros emitted by Python macro modules
during dbt project generation.

The runtime environment may provide:

- `inheritance_paths[]`: additional directories searched when resolving
  `extends`.
- `macro_paths[]`: directories containing custom Python macro `.py` files to
  load during validation and dbt project generation.

Runtime inheritance paths and runtime macro paths are implementation inputs such
as command-line options, environment configuration, or orchestrator parameters.
They are not part of the materialisation YAML document.

Before DBT-compilation time, the implementation must load custom
Python macros, generate the required dbt/Jinja macro artifacts, and make those
generated artifacts available under the transient dbt project's `macros/`
directory. Duplicate custom Python macro references across runtime macro paths
are invalid unless the reference is unambiguous.

Implementation code should not introduce behavior that is absent from this
specification without first updating this document.
