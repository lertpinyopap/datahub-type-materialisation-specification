# Type Materialisation Specification

## 1. Purpose

The Type Materialisation Specification defines a generic YAML document format
for describing how flat, raw, or schema-on-read data is transformed into typed
relational data.

The specification is designed to support CSV sources, table sources, reusable
table families through inheritance, and dbt-based materialisation.

## 2. Notation

This document uses a small grammar-like notation for the shape of the YAML:

```text
name ::= allowed_value | other_allowed_value
property? ::= optional property
property[] ::= list property
```

The grammar is descriptive. The formal static validator is the JSON Schema in
`schema/type-materialisation.schema.json`.

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

## 3. Overall Document

```text
type_materialisation_spec ::= complete_spec | extending_spec

complete_spec ::=
  id
  description?
  control_data?
  source
  target

extending_spec ::=
  id
  description?
  extends  # see section 15, Inheritance
  control_data?
  source?
  target?
```

Sample: complete CSV-backed materialisation specification.

```yaml
id: account_file_format
description: Maps raw account CSV data into a typed account table.
control_data:
  materialisation_type: table
  failure_mode: quarantine_row
source:
  format: csv
  separator: ","
  header: true
target:
  name: account
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
  materialisation_type?
  failure_mode?
  quarantine?
  job?

materialisation_type ::= view | materialised_view | table
failure_mode ::= fail_file | quarantine_row
quarantine ::= quarantine_table
job ::= job_table
```

`control_data` defines behavior for the whole materialisation.

`materialisation_type` defines the kind of typed output to produce. If omitted,
implementations should default to `table`.

`failure_mode` defines how validation or conversion failures are handled. If
omitted, implementations should default to `fail_file`.

`quarantine` optionally describes the quarantine table used when
`failure_mode` is `quarantine_row`.

`job` optionally describes the job table used to record load lifecycle events.

`materialisation_type` values:

- `view`: produce a typed view over the source data.
- `materialised_view`: produce a persisted or incrementally refreshed typed
  view.
- `table`: produce a typed table populated from the source data.

`failure_mode` values:

- `fail_file`: fail the whole materialisation when a record fails validation or
  type conversion.
- `quarantine_row`: write the failing record to quarantine output and continue
  processing subsequent records.

Sample: control data.

```yaml
control_data:
  materialisation_type: table
  failure_mode: quarantine_row
  quarantine:
    table: account_QUARANTINE
  job:
    table: account_JOB
```

### 5.1 Quarantine Table

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

If `quarantine.table` is omitted, implementations should default to
`<target.name>_QUARANTINE`.

If `quarantine.database` or `quarantine.schema` are omitted, implementations
should use their default database or schema resolution rules for generated
objects.

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
  failure_mode: quarantine_row
  quarantine:
    database: ops
    schema: data_quality
    table: account_load_QUARANTINE
```

### 5.2 Job Table

```text
job_table ::=
  database?
  schema?
  table?

database ::= string
schema ::= string
table ::= string

job_event_type ::= JOB_START | JOB_END
job_result ::= COMPLETED | COMPLETED_WITH_ERRORS | FAILED
```

The job table records event rows for each materialisation load. Each load emits
a `JOB_START` event and a `JOB_END` event. The two events for the same load
must share the same `job_id`.

If `job.table` is omitted, implementations should default to
`<target.name>_JOB`.

If `job.database` or `job.schema` are omitted, implementations should use their
default database or schema resolution rules for generated objects.

The job table must be created if it does not exist. If it already exists, it
must be expanded to cover the required job event columns, but it must never be
shrunk automatically.

The job event columns are:

- `job_id`: the identifier for a single materialisation load. This is shared by
  the load's `JOB_START` and `JOB_END` events and is referenced by quarantine
  rows for failures from the same load.
- `event_type`: the lifecycle event type.
- `event_timestamp`: the time the event occurred.
- `result`: the load result. `JOB_START` events should leave this value null.
  `JOB_END` events must set it to one of the `job_result` values.
- `caller_job_id`: the identifier supplied by the calling job system, such as an
  Airflow task id.

`job_event_type` values:

- `JOB_START`: the materialisation load has started.
- `JOB_END`: the materialisation load has ended.

`job_result` values:

- `COMPLETED`: the materialisation load completed without validation,
  conversion, or runtime failures.
- `COMPLETED_WITH_ERRORS`: the materialisation load completed after writing one
  or more failed records to quarantine output.
- `FAILED`: the materialisation load did not complete successfully.

The quarantine table retains its own `loaded_at`, `job_id`, and
`failure_details` metadata columns so failed rows can be inspected directly
while still tying back to the exact load lifecycle details in the job table.

Sample: job table configuration.

```yaml
control_data:
  job:
    database: ops
    schema: data_quality
    table: account_JOB
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
- `table`: relational table input where fields are selected by column name.

### 6.1 CSV Source

```text
csv_source ::=
  format: csv
  header
  separator?
  quote_char?
  row_terminator?
  quoting?

header ::= true | false
separator ::= one character string
quote_char ::= one character string
row_terminator ::= string
quoting ::= minimal | all | non_numeric | none | notnull | strings
```

CSV dialect options align with Python CSV dialect concepts:

- `separator`: field separator character, equivalent to Python `delimiter`.
  Defaults to `,`.
- `quote_char`: field quoting character, equivalent to Python `quotechar`.
  Defaults to `"`.
- `row_terminator`: row terminator string, equivalent to Python
  `lineterminator`. Defaults to `\r\n`.
- `quoting`: how the reader interprets quoted fields, equivalent to Python
  `quoting`. Defaults to `minimal`.

`quoting` values:

- `minimal`: parse quote characters only where needed for fields containing
  special characters.
- `all`: parse all fields as quoted fields.
- `non_numeric`: parse quoted fields as non-numeric values and unquoted fields
  as numeric values where supported.
- `none`: do not treat quote characters specially while reading.
- `notnull`: parse quoted fields as non-null values and unquoted empty fields
  as null where supported.
- `strings`: parse quoted fields as string values where supported.

Sample: CSV source.

```yaml
source:
  format: csv
  header: true
  separator: ","
  quote_char: '"'
  row_terminator: "\r\n"
  quoting: minimal
```

### 6.2 Table Source

```text
table_source ::=
  format: table
  database
  schema
  table

database ::= string
schema ::= string
table ::= string
```

Sample: table source.

```yaml
source:
  format: table
  database: raw
  schema: landing
  table: account_file_landed
```

## 7. Target

```text
target ::=
  name
  fields[]

name ::= string
```

`target` describes the typed data produced by the materialisation.

`target.fields` must contain at least one field.

Sample: target table definition.

```yaml
target:
  name: account
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
  unique?
  validations?

id ::= identifier
nullable ::= true | false
unique ::= true | false
```

`target.fields` is an ordered list of fields to materialise in the target.
Each field `id` is an identifier and must be unique within the resolved target
field list.

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
    unique: true
```

## 9. Field Source

```text
field.source ::= csv_field_source | table_field_source

csv_field_source ::=
  pos?
  column?

table_field_source ::=
  column

pos ::= integer >= 0
column ::= string
```

For CSV sources, a field may be selected by position, column name, or both.

For table sources, a field must be selected by column name. `pos` is invalid for
table sources.

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

Reference implementations should parse and validate supported data types rather
than treating them as arbitrary strings.

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

format ::= Python datetime format string

round ::=
  type: round
  scale
  mode?

scale ::= integer >= 0
mode ::= half_up | half_even | down | up

custom ::=
  type: custom
  macro

macro ::= dbt macro name
```

Transforms are simple single-column instructions applied to the extracted field
value. They must not reference other source fields or target fields.

Transforms are applied in the order they appear. A transform output must be
compatible with the field's `data_type`.

`transform_rule.type` values:

- `trim`: remove whitespace from a string value.
- `parse_date`: parse a string value into a date using `format`.
- `parse_timestamp`: parse a string value into a timestamp using `format`.
- `round`: round a numeric value to a decimal `scale`.
- `custom`: apply a named dbt macro to the column.

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

Date and timestamp `format` values use Python `datetime` `strptime` /
`strftime`-style format codes. dbt reference implementations should map these
format strings to database-native parsing functions where required.

Common date and timestamp format examples:

- `%Y-%m-%d`: `2026-08-13`
- `%d/%m/%Y`: `13/08/2026`
- `%Y-%m-%d %H:%M:%S`: `2026-08-13 14:30:00`
- `%Y-%m-%dT%H:%M:%S%z`: `2026-08-13T14:30:00+1000`

Custom transforms are a constrained dbt extension point. `macro` names a dbt
macro that is expected to be available to the dbt project at runtime. The macro
is applied to the current column only.

```text
custom_transform_macro ::= macro_name(column_expression)
custom_transform_macro_result ::= SQL expression returning transformed value
```

The dbt macro receives the SQL expression for the current column and returns a
SQL expression for the transformed column.

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

Sample: custom dbt macro transform.

```yaml
fields:
  - id: account_id
    source:
      column: account_id
    data_type: varchar(20)
    transforms:
      - type: custom
        macro: normalise_account_id
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
macro ::= dbt macro name
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

Custom validation macro signature:

```text
custom_validation_macro ::= macro_name(column_expression)
custom_validation_macro_result ::= SQL expression returning nullable string
```

A custom validation returns no failure when it returns `NULL`. It returns
failure details when it returns a string. The string should explain the first
failure detected for the current column value.

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
    macro: validate_account_id
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
```

Variable expressions are resolved after YAML parsing and before source loading.

Sample: table source with dbt-style variables.

```yaml
source:
  format: table
  database: "{{ env_var('RAW_DATABASE') }}"
  schema: "{{ var('raw_schema', 'landing') }}"
  table: "{{ env_var('ENV') }}_account_file_landed"
```

## 14. Validation Phases

```text
SCHEMA time ::= YAML parses and conforms to JSON Schema
PARSE time ::= rules that require the parsed YAML document
INHERITANCE time ::= parent specifications are loaded and overlaid
RESOLVE time ::= rules that require variable resolution
LOAD time ::= rules that require source data or source metadata
```

Schema-time rules are enforced by
`schema/type-materialisation.schema.json`.

Parse-time rules:

- `source.format ::= csv | table`.
- For `source.format = csv`, each field source must specify at least one of
  `pos` or `column`.
- For `source.format = csv` and `source.header = false`, `field.source.column`
  is invalid.
- For `source.format = table`, `field.source.column` is required.
- For `source.format = table`, `field.source.pos` is invalid.

Inheritance-time rules:

- Parent specifications referenced by `extends` must be available before
  variable expressions are resolved.
- The resolved specification after inheritance must be complete.

Resolve-time rules:

- Supported variable expressions must resolve to strings.
- A variable with no value and no default fails resolution.
- Unsupported Jinja expressions are invalid.
- Custom transform macros must be available to dbt implementations before the
  materialisation is executed.

Load-time rules:

- For `source.format = csv`, if `field.source.pos` and `field.source.column`
  are both specified, the source header at `pos` must equal `column`.
- For `source.format = table`, `field.source.column` must exist in the source
  table.

## 15. Inheritance

```text
extends ::= parent_specification_id
parent_specification_id ::= identifier

parent_specification_path ::= same_directory + "/" + parent_specification_id + ".yaml"
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
  failure_mode: quarantine_row
source:
  format: table
  database: raw
  schema: reference_data
  table: abstract_reference_code_data
target:
  name: abstract_reference_code_data
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
  name: customer_reference_code_data
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
- Parent specifications are expected to be available in the same folder as the
  child specification and named `<id>.yaml`.
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

## 16. Reference Implementations

The reference implementation should produce dbt artifacts that perform
materialisation, transforms, validations, quarantine handling, and target writes.

Python may be used for repository tooling such as YAML parsing, JSON Schema
validation, inheritance resolution, dbt artifact generation, golden-file tests,
and test harnesses. Python is not a separate materialisation runtime for this
specification.

Reference implementation code should not introduce behavior that is absent from
this specification without first updating this document.
