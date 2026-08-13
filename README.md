# DataHub Type Materialisation

This repository contains both the Type Materialisation Specification and its
Python reference implementation.

The specification defines a generic YAML format for describing how flat or
schema-on-read data is transformed into typed relational structures such as
views, materialised views, and tables. The reference implementation provides the
`tms` command-line tool for parsing specifications and validating CSV inputs.

The specification is intended to support both:

- CSV-style positional mapping from delimited files.
- Raw or semi-structured ingestion patterns where engineering teams land data
  first and apply schema later.

The goal is to make type materialisation explicit, reviewable, and reusable. A
single specification should describe the source format, target fields, typing
rules, validation rules, error handling, and any shared inheritance pattern used
across related tables.

The reference implementation is expected to generate and validate dbt artifacts.
Python is used for parsing, validation, macro handling, and generation support;
dbt remains the materialisation runtime.

## Documentation

- [SPEC.md](./SPEC.md) contains the formal Type Materialisation Specification.
- [schema/type-materialisation.schema.json](./schema/type-materialisation.schema.json)
  contains the JSON Schema for static concrete specification validation.
- [schema/type-materialisation-abstract.schema.json](./schema/type-materialisation-abstract.schema.json)
  contains the JSON Schema for static abstract specification shape validation.
- [samples/yaml/](./samples/yaml) contains valid concrete and abstract sample
  specifications, with intentionally invalid examples in
  [samples/yaml/broken/](./samples/yaml/broken).
- [samples/csv/](./samples/csv) contains small CSV fixtures for CSV-backed YAML
  specifications, with intentionally invalid examples in
  [samples/csv/broken/](./samples/csv/broken).
- [src/type_materialisation/](./src/type_materialisation) contains the Python
  reference implementation.
- [macros/](./macros) contains sample Python macro objects used by the samples.
- [AGENTS.md](./AGENTS.md) contains working instructions for Codex and other
  repository agents.

## Reference Implementation

The Python package is installed as an editable local package and exposes the
`tms` command.

Local development uses the root `requirements.txt`, which installs the dbt
1.12 Snowflake adapter line.

Install:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install --no-build-isolation --no-deps -e .
```

On macOS ARM, if `pip install -r requirements.txt` fails while preparing
metadata for `dbt-core-experimental-parser`, install the dbt parser wheel
directly first, then rerun the requirements install:

```bash
python -m pip install \
  "dbt-core-experimental-parser @ https://github.com/dbt-labs/dbt-core/releases/download/v2.0.0-beta.1/dbt_core_experimental_parser-2.0.0b1-py3-none-macosx_11_0_arm64.whl"
python -m pip install -r requirements.txt
python -m pip install --no-build-isolation --no-deps -e .
```

Run parser checks:

```bash
tms parse --spec samples/yaml/account_csv.yaml
tms parse --abstract --spec samples/yaml/customer_reference_code_data.yaml
tms parse --spec samples/yaml/broken/account_csv_broken.yaml
tms parse --spec path/to/child.yaml --spec-path path/to/parents
```

Run CSV validation:

```bash
tms validate --spec samples/yaml/account_csv.yaml --input-file samples/csv/account_csv.csv
tms validate --spec samples/yaml/account_csv.yaml --input-file samples/csv/broken/account_csv_bad_account_number.csv
tms validate --spec samples/yaml/account_csv.yaml --input-file samples/csv/broken/account_csv_header_mismatch.csv
tms validate --spec path/to/child.yaml --spec-path path/to/parents --input-file path/to/input.csv
```

Generate a dbt project:

```bash
tms generate-dbt --spec samples/yaml/account_csv.yaml --unit-test-csv samples/csv/account_csv.csv
tms generate-dbt --spec samples/yaml/account_csv.yaml --output-dir /tmp/tms-dbt-account --csv-stage RAW.PUBLIC.ACCOUNT_STAGE
tms generate-dbt --spec path/to/child.yaml --spec-path path/to/parents
```

If `--output-dir` is omitted, `tms generate-dbt` writes to a fresh temporary
directory. CSV stage details come from `source.location` in the spec unless
`--csv-stage` is supplied. Any omitted database is resolved by the active dbt
adapter/session context. CSV upload is planned as a Python `tms` step outside
dbt generation, but is not implemented yet. The generated project currently
covers CSV-stage and table-source materialisation slices, including generated
job run hooks and append-only quarantine models; see
[IMPLEMENTATION_STATUS.md](./IMPLEMENTATION_STATUS.md) for unsupported features.

Custom macros are Python objects referenced by dotted path from YAML. They must
generate dbt/Jinja SQL and may optionally support local Python execution for
`tms validate`.

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
  delimiter: ","
  quotechar: '"'
  lineterminator: "\r\n"
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
