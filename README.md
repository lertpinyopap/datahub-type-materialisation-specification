# DataHub Type Materialisation Specification

This repository defines a generic YAML specification for describing how flat or
schema-on-read data is transformed into typed relational structures such as
views, materialised views, and tables.

The specification is intended to support both:

- CSV-style positional mapping from delimited files.
- Raw or semi-structured ingestion patterns where engineering teams land data
  first and apply schema later.

The goal is to make type materialisation explicit, reviewable, and reusable. A
single specification should describe the source format, target fields, typing
rules, validation rules, error handling, and any shared inheritance pattern used
across related tables.

The reference implementation is expected to generate and validate dbt artifacts.
Python may be used for supporting tooling, but dbt is the materialisation
runtime.

## Documentation

- [SPEC.md](./SPEC.md) contains the formal Type Materialisation Specification.
- [schema/type-materialisation.schema.json](./schema/type-materialisation.schema.json)
  contains the JSON Schema for static concrete specification validation.
- [schema/type-materialisation-abstract.schema.json](./schema/type-materialisation-abstract.schema.json)
  contains the JSON Schema for static abstract specification shape validation.
- [samples/](./samples) contains valid concrete and abstract sample
  specifications.
- [AGENTS.md](./AGENTS.md) contains working instructions for Codex and other
  repository agents.

## Example

Sample: CSV account file materialisation.

```yaml
id: account_file_format
description: Account file mapping.
control_data:
  materialisation_type: table
  failure_mode: quarantine_row
  quarantine:
    table: account_QUARANTINE
source:
  format: csv
  header: true
  separator: ","
  quote_char: '"'
  row_terminator: "\r\n"
  quoting: minimal
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
      transforms:
        - type: trim
      unique: true
      nullable: false
      validations:
        - type: min_length
          value: 16
        - type: max_length
          value: 20
```
