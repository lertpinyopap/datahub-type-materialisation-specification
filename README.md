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

Reference implementations are expected to be written in Python and dbt as the
specification matures.

## Documentation

- [SPEC.md](./SPEC.md) contains the formal Type Materialisation Specification.
- [schema/type-materialisation.schema.json](./schema/type-materialisation.schema.json)
  contains the JSON Schema for static YAML validation.
- [samples/](./samples) contains valid sample specifications.
- [AGENTS.md](./AGENTS.md) contains working instructions for Codex and other
  repository agents.

## Example

```yaml
id: account_file_format
description: Account file mapping.
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
      unique: true
      nullable: false
      validations:
        - type: min_length
          value: 16
        - type: max_length
          value: 20
```
