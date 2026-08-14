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

## What Gets Tested

The normal unit suite exercises the integration framework without connecting to
Snowflake. It checks that scenarios are discoverable, generated projects are
well formed, prefixed relation names are used, generated dbt unit tests contain
the expected fixtures, and runner safeguards such as cleanup and prefix
validation behave as intended.

The live integration suite executes each scenario against Snowflake. For each
scenario, the runner:

1. Generates a transient dbt project from the scenario spec.
2. Prefixes all generated live relations with `TMS_INTEGRATION_TABLE_PREFIX`.
3. Cleans up the scenario's prefixed relations before the test starts.
4. Creates any declared initial target table and rows.
5. Replaces the generated seed with each load fixture.
6. Runs `tms dbt-build`.
7. Reads the target table and compares it with the expected CSV.
8. Cleans up generated relations unless `TMS_INTEGRATION_KEEP_TABLES=1`.

The generated dbt unit tests validate first-load transformation behavior inside
the generated dbt project. Live scenario assertions validate database end state
after dbt has run, including incremental behavior that depends on existing
target rows.

Live runs print Rich-formatted progress directly to the terminal, including the
scenario name, setup steps, dbt generation/build steps, target-row checks, and
cleanup behavior. Set `TMS_INTEGRATION_PROGRESS=0` to silence this progress
output.

Live database connection settings are read from `~/.snowflake/config.toml`.
The runner reads Snowflake CLI-style `[connections.<name>]` entries and defaults
to `[connections.tms_int]`.

Optional variables:

- `TMS_SNOWFLAKE_CONNECTION`, to use a profile other than `tms_int`.
- `TMS_INTEGRATION_SCHEMA`, default `TMP`
- `TMS_INTEGRATION_TABLE_PREFIX`, default `TMS_INT__`; must be at least three
  characters long and end with `__`.
- `TMS_INTEGRATION_KEEP_TABLES=1` keeps generated relations after a successful
  or failed run for inspection.
- `TMS_INTEGRATION_PROGRESS=0` disables live progress output.
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
