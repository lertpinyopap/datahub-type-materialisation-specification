# TMS Integration Tests

These tests exercise generated dbt projects against a live Snowflake database.
They are intentionally scenario-folder driven: each scenario owns its
documentation, specification, data, and any scenario-specific code.

Run the offline framework tests with the normal unit suite:

```sh
.venv/bin/python -m pytest
```

Run live integration scenarios explicitly:

```sh
TMS_RUN_INTEGRATION=1 .venv/bin/python -m pytest integration_tests
```

Required live environment variables:

- `TMS_SNOWFLAKE_ACCOUNT`
- `TMS_SNOWFLAKE_USER`
- `TMS_SNOWFLAKE_WAREHOUSE`
- `TMS_SNOWFLAKE_DATABASE`

Authentication can use either:

- `TMS_SNOWFLAKE_PASSWORD`
- `TMS_SNOWFLAKE_AUTHENTICATOR`, for example `externalbrowser`

Optional variables:

- `TMS_SNOWFLAKE_ROLE`
- `TMS_INTEGRATION_SCHEMA`, default `TMP`
- `TMS_INTEGRATION_TABLE_PREFIX`, default `TMS_INT__`
- `TMS_INTEGRATION_KEEP_TABLES=1` keeps generated relations after a successful
  or failed run for inspection.
- `DBT_PROFILES_DIR`, if the generated project should use a non-default dbt
  profiles directory.

The live runner creates the target schema when needed but does not drop it.
Cleanup is relation-scoped within `TMS_INTEGRATION_SCHEMA`. A pre-run cleanup
always removes the scenario's prefixed relations so reruns start from a clean
state, even if a previous run used `TMS_INTEGRATION_KEEP_TABLES=1`. Final cleanup
runs by default and is skipped only when `TMS_INTEGRATION_KEEP_TABLES=1`.
Generated live relations are prefixed with `TMS_INTEGRATION_TABLE_PREFIX` to
reduce the chance of clashing with other objects in `TMP`.

Scenario folders follow this structure:

```text
integration_tests/<scenario_name>/
  README.md
  scenario.yaml
  spec/
    account.yaml
  data/
    load_001_source.csv
    expected_after_load_001.csv
  src/
    README.md
```

`integration_tests/src/tms_integration/` contains the reusable Python runner.
Scenario `src/` folders are reserved for scenario-specific SQL, Python, or notes
when a case needs custom setup or assertions.
