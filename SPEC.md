# Type Materialisation Specification

## 1. Purpose

The Type Materialisation Specification defines a generic YAML document format
for describing how flat, raw, or schema-on-read data is transformed into typed
relational data.

The specification is designed to support CSV sources, table sources, reusable
table families through inheritance, and reference implementations in Python and
dbt.

## 2. Notation

This document uses a small grammar-like notation for the shape of the YAML:

```text
name ::= allowed_value | other_allowed_value
property? ::= optional property
property[] ::= list property
```

The grammar is descriptive. The formal static validator is the JSON Schema in
`schema/type-materialisation.schema.json`.

## 3. Overall Document

```text
type_materialisation_spec ::=
  id
  description?
  extends?
  control_data?
  source
  target
```

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
```

## 4. Identity

```text
id ::= string
description ::= string
extends ::= string
```

`id` is the stable identifier for the specification.

`description` is human-readable documentation for the specification.

`extends` identifies a parent specification to inherit from.

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

materialisation_type ::= view | materialised_view | table
failure_mode ::= fail_file | quarantine_row
```

`control_data` defines behavior for the whole materialisation.

`materialisation_type` defines the kind of typed output to produce. If omitted,
implementations should default to `table`.

`failure_mode` defines how validation or conversion failures are handled. If
omitted, implementations should default to `fail_file`.

```yaml
control_data:
  materialisation_type: table
  failure_mode: quarantine_row
```

## 6. Source

```text
source ::= csv_source | table_source
source.format ::= csv | table
```

`source` describes the raw data being read before target field extraction,
typing, and validation.

### 6.1 CSV Source

```text
csv_source ::=
  format: csv
  separator
  header

separator ::= string
header ::= true | false
```

```yaml
source:
  format: csv
  separator: ","
  header: true
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
  nullable?
  unique?
  validations?

nullable ::= true | false
unique ::= true | false
```

`target.fields` is an ordered list of fields to materialise in the target.

```yaml
fields:
  - id: account_id
    source:
      pos: 0
      column: account_id
    data_type: varchar(20)
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

```yaml
fields:
  - id: account_id
    source:
      pos: 0
      column: account_id
    data_type: varchar(20)
```

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

## 11. Validation Rules

```text
validations ::= validation_rule[]

validation_rule ::=
  type
  validator_specific_properties?

type ::= string
```

Initial validator examples:

```yaml
validations:
  - type: min_length
    value: 16
  - type: max_length
    value: 20
```

Validator behavior will be documented as validators are added. The JSON Schema
validates the shape of validation rules, not the runtime behavior of each
validator.

## 12. Variable Usage

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

```yaml
source:
  format: table
  database: "{{ env_var('RAW_DATABASE') }}"
  schema: "{{ var('raw_schema', 'landing') }}"
  table: "{{ env_var('ENV') }}_account_file_landed"
```

## 13. Validation Phases

```text
SCHEMA time ::= YAML parses and conforms to JSON Schema
PARSE time ::= rules that require the parsed YAML document
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

Resolve-time rules:

- Supported variable expressions must resolve to strings.
- A variable with no value and no default fails resolution.
- Unsupported Jinja expressions are invalid.

Load-time rules:

- For `source.format = csv`, if `field.source.pos` and `field.source.column`
  are both specified, the source header at `pos` must equal `column`.
- For `source.format = table`, `field.source.column` must exist in the source
  table.

## 14. Inheritance

```text
extends ::= parent_specification_id
```

Inheritance allows a specification to reuse a shared baseline.

```yaml
id: customer_reference_code_data
extends: abstract_reference_code_data
description: Customer reference code data.
```

Initial inheritance rules:

- A child specification references one parent through `extends`.
- Parent specifications may be abstract or concrete.
- Child scalar properties override parent scalar properties.
- Child mapping properties are merged with parent mapping properties.
- Child target field definitions are matched to parent target field definitions
  by `id`.
- A child target field may override inherited field properties.
- A child may add target fields not present in the parent.

Open question: whether a child may remove inherited fields should be confirmed
before implementation.

Open question: whether multiple inheritance is required should be confirmed
before implementation.

## 15. Reference Implementations

Reference implementations should be added for Python and dbt.

The Python reference implementation should focus on parsing, validating, and
executing the specification against local or test source data.

The dbt reference implementation should focus on generating or validating
database-native materialisation logic from the specification.

Reference implementations should not introduce behavior that is absent from
this specification without first updating this document.
