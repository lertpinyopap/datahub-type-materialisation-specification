"""Project, governance, macro, and runtime-hook generation."""
from __future__ import annotations

from importlib.resources import files
import json
from pathlib import Path
import sys
from typing import Any

# Airflow may load this runtime helper from its file path rather than as a
# package module. Make the package root available before resolving relative
# imports in that mode.
if __package__ in {None, ""}:
    _package_root = Path(__file__).resolve().parent.parent
    if str(_package_root) not in sys.path:
        sys.path.insert(0, str(_package_root))
    __package__ = "type_materialisation"

from .schema import require_yaml
from .spec import GENERATED_METADATA_FIELD_TYPES, fields
from .dbt_sql import (
    _count_cte, _dbt_relation_lookup, _job_id_expression, _job_relation_config,
    _nullable_sql_string, _physical_name, _quarantine_has_explicit_schema, _quote_identifier,
    _quarantine_relation_config, _relation_name, _runtime_relation_label,
    _runtime_sql_string, _sql_scalar, _sql_string, _target_relation_config,
)

DBT_PROJECT_NAME = "type_materialisation_generated"
DBT_PROFILE_NAME = "datahub_type_materialisation"


def _write(path: Path, content: str, result: Any) -> None:
    path.write_text(content, encoding="utf-8")
    result.files.append(path)


def _quarantine_enabled(spec: dict[str, Any]) -> bool:
    control_data = spec.get("control_data", {})
    return not (isinstance(control_data, dict) and control_data.get("validation_enabled") is False) and isinstance(control_data, dict) and control_data.get("failure_mode") == "quarantine_row"


def _validation_guard_model_name(target_id: str) -> str:
    return f"{target_id}__validation_guard"


def _seed_project_config(spec: dict[str, Any]) -> dict[str, Any] | None:
    from .dbt_source import _seed_project_config as renderer
    return renderer(spec)

def _write_project_file(output_dir: Path, spec: dict[str, Any], spec_file_name: str, result: DbtGenerationResult) -> None:
    yaml = require_yaml()
    pre_hooks, post_hooks = _job_hooks(spec, spec_file_name)
    project = {
        "name": DBT_PROJECT_NAME,
        "version": "1.0",
        "config-version": 2,
        "profile": DBT_PROFILE_NAME,
        "model-paths": ["models"],
        "seed-paths": ["seeds"],
        "macro-paths": ["macros"],
        "on-run-start": pre_hooks,
        "on-run-end": post_hooks,
        "models": {
            DBT_PROJECT_NAME: {
                "generated": {
                    "+materialized": "view",
                }
            }
        },
    }
    seed_config = _seed_project_config(spec)
    if seed_config is not None:
        project["seeds"] = seed_config
    content = yaml.safe_dump(project, sort_keys=False, width=10_000)
    _write(output_dir / "dbt_project.yml", content, result)



def _tag_post_hook_lines(spec: dict[str, Any]) -> list[str]:
    """Apply target governance after the target relation exists."""
    target = spec.get("target", {})
    if not isinstance(target, dict):
        return []

    if target.get("tag_application", "alter_table") == "apply_governance":
        statements = _governance_contract_merge_statements(spec)
        procedure = target.get("apply_governance_procedure")
        if not isinstance(procedure, str):
            return _post_hook_config_lines(statements)
        statements.append(
            f"call {_tag_identifier(procedure)}("
            "'{{ this.database | upper }}', "
            "'{{ this.schema | upper }}', "
            "'{{ this.identifier | upper }}')"
        )
        return _post_hook_config_lines(statements)

    statements: list[str] = []
    target_tags = target.get("tags", {})
    if isinstance(target_tags, dict):
        for tag_name, tag_value in target_tags.items():
            statements.append(
                "alter table {{ this }} set tag "
                f"{_tag_identifier(tag_name)} = {_sql_string(str(tag_value))}"
            )

    for field in fields(spec):
        field_tags = field.get("tags", {}) if isinstance(field, dict) else {}
        if not isinstance(field_tags, dict):
            continue
        for tag_name, tag_value in field_tags.items():
            statements.append(
                "alter table {{ this }} modify column "
                f"{_quote_identifier(str(field['id']))} set tag "
                f"{_tag_identifier(tag_name)} = {_sql_string(str(tag_value))}"
            )

    return _post_hook_config_lines(statements)



def _governance_contract_merge_statements(spec: dict[str, Any]) -> list[str]:
    """Return one idempotent contract upsert per classified target field."""
    target = spec.get("target", {})
    if not isinstance(target, dict):
        return []
    contract = target.get("governance_contract")
    if not isinstance(contract, dict):
        return []
    contract_table = contract.get("table")
    if not isinstance(contract_table, str):
        return []

    contract_source = contract.get("source", f"dbt:{target.get('id', spec.get('id', 'target'))}")
    contract_version = contract.get("version", "1")
    statements: list[str] = []
    for field in fields(spec):
        values = _governance_contract_field_values(field)
        if values is None:
            continue
        statements.append(
            _governance_contract_merge_statement(
                contract_table=contract_table,
                column_name=str(field["id"]),
                pii_category=values["PII_CATEGORY"],
                pci_category=values["PCI_CATEGORY"],
                data_classification=values["DATA_CLASSIFICATION"],
                description=field.get("description"),
                contract_source=contract_source,
                contract_version=contract_version,
            )
        )
    return statements



def _governance_contract_field_values(field: dict[str, Any]) -> dict[str, Any] | None:
    tags = field.get("tags", {})
    if not isinstance(tags, dict):
        tags = {}
    values = {"PII_CATEGORY": None, "PCI_CATEGORY": None, "DATA_CLASSIFICATION": None}
    for tag_name, tag_value in tags.items():
        category = str(tag_name).rsplit(".", maxsplit=1)[-1].upper()
        if category in values:
            values[category] = tag_value
    if any(value is not None for value in values.values()) or field.get("description") is not None:
        return values
    return None



def _governance_contract_merge_statement(
    *,
    contract_table: str,
    column_name: str,
    pii_category: Any,
    pci_category: Any,
    data_classification: Any,
    description: Any,
    contract_source: Any,
    contract_version: Any,
) -> str:
    """Build the Snowflake MERGE used to register one contract column."""
    source_columns = [
        ("'{{ this.database | upper }}'", "database_name"),
        ("'{{ this.schema | upper }}'", "schema_name"),
        ("'{{ this.identifier | upper }}'", "table_name"),
        (_sql_string(column_name), "column_name"),
        (_sql_scalar(pii_category), "pii_category"),
        (_sql_scalar(pci_category), "pci_category"),
        (_sql_scalar(data_classification), "data_classification"),
        (_sql_scalar(description), "description"),
        (_sql_scalar(contract_source), "contract_source"),
        (_sql_scalar(contract_version), "contract_version"),
    ]
    select_columns = ", ".join(f"{value} as {name}" for value, name in source_columns)
    return (
        f"merge into {_tag_identifier(contract_table)} as t using (select {select_columns}) as s "
        "on t.database_name = s.database_name "
        "and t.schema_name = s.schema_name "
        "and t.table_name = s.table_name "
        "and t.column_name = s.column_name "
        "when matched then update set "
        "t.pii_category = s.pii_category, "
        "t.pci_category = s.pci_category, "
        "t.data_classification = s.data_classification, "
        "t.description = s.description, "
        "t.contract_source = s.contract_source, "
        "t.contract_version = s.contract_version, "
        "t.loaded_at = current_timestamp(), "
        "t.loaded_by = current_role() "
        "when not matched then insert "
        "(database_name, schema_name, table_name, column_name, pii_category, pci_category, "
        "data_classification, description, contract_source, contract_version, loaded_at, loaded_by) "
        "values (s.database_name, s.schema_name, s.table_name, s.column_name, s.pii_category, "
        "s.pci_category, s.data_classification, s.description, s.contract_source, "
        "s.contract_version, current_timestamp(), current_role())"
    )



def _post_hook_config_lines(statements: list[str]) -> list[str]:
    if not statements:
        return []
    lines = ["    post_hook=["]
    lines.extend(f"        {json.dumps(statement)}," for statement in statements)
    lines.append("    ],")
    return lines



def _tag_identifier(value: Any) -> str:
    """Keep qualified Snowflake tag names usable while normalizing simple names."""
    text = str(value).strip()
    if not text:
        raise ValueError("tag name must not be empty")
    return ".".join(_quote_identifier(part) for part in text.split("."))



def _write_generated_macros(
    output_dir: Path,
    macros: dict[str, str],
    spec: dict[str, Any],
    result: DbtGenerationResult,
) -> None:
    _write(output_dir / "macros" / "generated" / "create_schema.sql", _create_schema_macro(), result)
    _write(output_dir / "macros" / "generated" / "generate_schema_name.sql", _generate_schema_name_macro(), result)
    bookmark = _incremental_bookmark_config(spec)
    if bookmark is not None:
        _write(
            output_dir / "macros" / "generated" / "incremental_bookmark.sql",
            _render_incremental_bookmark_macro(bookmark),
            result,
        )
    for macro_name, macro_sql in sorted(macros.items()):
        _write(output_dir / "macros" / "generated" / f"{macro_name}.sql", macro_sql + "\n", result)



def _create_schema_macro() -> str:
    return "\n".join(
        [
            "{% macro create_schema(relation) -%}",
            "    {# Schemas must be provisioned outside generated TMS dbt projects. #}",
            "{%- endmacro %}",
            "",
        ]
    )



def _generate_schema_name_macro() -> str:
    return "\n".join(
        [
            "{% macro generate_schema_name(custom_schema_name, node) -%}",
            "    {%- if custom_schema_name is none -%}",
            "        {{ target.schema | upper }}",
            "    {%- else -%}",
            "        {{ custom_schema_name | trim | upper }}",
            "    {%- endif -%}",
            "{%- endmacro %}",
            "",
        ]
    )



def _incremental_bookmark_config(spec: dict[str, Any]) -> dict[str, str] | None:
    control_data = spec.get("control_data", {})
    raw = control_data.get("incremental_bookmark") if isinstance(control_data, dict) else None
    if not isinstance(raw, dict):
        return None
    bookmark_relation = raw.get("bookmark_relation")
    source_relation = raw.get("source_relation")
    if not isinstance(bookmark_relation, str) or not isinstance(source_relation, str):
        return None
    return {
        "bookmark_relation": bookmark_relation,
        "source_relation": source_relation,
        "source_timestamp_column": str(raw.get("source_timestamp_column", "AIRFLOW_DAG_TIME")),
        "pipeline_name": str(raw.get("pipeline_name", spec["id"])),
    }



def _render_incremental_bookmark_macro(bookmark: dict[str, str]) -> str:
    """Render the versioned dbt macro template with spec-specific relations."""
    template = files("type_materialisation").joinpath(
        "templates", "incremental_bookmark.sql"
    ).read_text(encoding="utf-8")
    replacements = {
        "__BOOKMARK_RELATION__": bookmark["bookmark_relation"],
        "__SOURCE_RELATION__": bookmark["source_relation"],
        "__SOURCE_RELATION_LITERAL__": _runtime_sql_string(bookmark["source_relation"]),
        "__SOURCE_TIMESTAMP_COLUMN__": _physical_name(bookmark["source_timestamp_column"]),
        "__PIPELINE_NAME_LITERAL__": _sql_string(bookmark["pipeline_name"]),
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template



def _incremental_bookmark_hooks(spec: dict[str, Any]) -> tuple[list[str], list[str]]:
    bookmark = _incremental_bookmark_config(spec)
    if bookmark is None:
        return [], []
    relation = bookmark["bookmark_relation"]
    source_relation = bookmark["source_relation"]
    timestamp_column = _physical_name(bookmark["source_timestamp_column"])
    pipeline_name = _sql_string(bookmark["pipeline_name"])
    create_sql = (
        f"create table if not exists {relation} ("
        "PIPELINE_NAME varchar not null, SOURCE_RELATION varchar not null, "
        "LAST_SOURCE_TIMESTAMP timestamp_ntz, "
        "UPDATED_AT timestamp_ntz not null default current_timestamp(), "
        "UPDATED_BY varchar not null default current_role())"
    )
    migrate_sql = f"alter table {relation} add column if not exists LAST_SOURCE_TIMESTAMP timestamp_ntz"
    advance_sql = "".join(
        [
            "{% set tms_bookmark_failed_count = ",
            "(results | selectattr('status', 'equalto', 'error') | list | length) + ",
            "(results | selectattr('status', 'equalto', 'fail') | list | length) %}",
            "{% if tms_bookmark_failed_count == 0 %}",
            f"merge into {relation} as target using (",
            f"select {pipeline_name} as PIPELINE_NAME, {_runtime_sql_string(source_relation)} as SOURCE_RELATION, ",
            f"max({timestamp_column}) as LAST_SOURCE_TIMESTAMP from {source_relation} ",
            f"having max({timestamp_column}) is not null",
            ") as source on target.PIPELINE_NAME = source.PIPELINE_NAME ",
            "and target.SOURCE_RELATION = source.SOURCE_RELATION ",
            "when matched then update set LAST_SOURCE_TIMESTAMP = source.LAST_SOURCE_TIMESTAMP, ",
            "UPDATED_AT = current_timestamp()::timestamp_ntz, UPDATED_BY = current_role() ",
            "when not matched then insert (PIPELINE_NAME, SOURCE_RELATION, LAST_SOURCE_TIMESTAMP, UPDATED_AT, UPDATED_BY) ",
            "values (source.PIPELINE_NAME, source.SOURCE_RELATION, source.LAST_SOURCE_TIMESTAMP, ",
            "current_timestamp()::timestamp_ntz, current_role())",
            "{% else %}select 1 where false{% endif %}",
        ]
    )
    return [create_sql, migrate_sql], [create_sql, migrate_sql, advance_sql]



def _job_hooks(spec: dict[str, Any], spec_file_name: str) -> tuple[list[str], list[str]]:
    relation = _relation_name(_job_relation_config(spec))
    target_relation = _target_relation_config(spec)
    generated_table = _runtime_relation_label(target_relation, schema_var="target_schema")
    quarantine_relation = _quarantine_relation_config(spec) if _quarantine_enabled(spec) else None
    quarantine_schema_var = None if _quarantine_has_explicit_schema(spec) else "tms_staging_schema"
    quarantine_table = (
        _runtime_relation_label(quarantine_relation, schema_var=quarantine_schema_var)
        if quarantine_relation is not None
        else None
    )
    generated_relation_lookup = _dbt_relation_lookup(
        target_relation,
        "tms_generated_relation",
        schema_var="target_schema",
    )
    quarantine_relation_lookup = (
        _dbt_relation_lookup(
            quarantine_relation,
            "tms_quarantine_relation",
            schema_var=quarantine_schema_var,
        )
        if quarantine_relation is not None
        else "{% set tms_quarantine_relation = none %}"
    )
    result_expression = (
        "{% set failed_result_count = "
        "(results | selectattr('status', 'equalto', 'error') | list | length) + "
        "(results | selectattr('status', 'equalto', 'fail') | list | length) %}"
        "{% if failed_result_count > 0 %}'FAILED'{% else %}"
        "case when quarantine_counts.QUARANTINE_COUNT > 0 "
        "then 'COMPLETED_WITH_QUARANTINE' else 'COMPLETED' end{% endif %}"
    )
    failed_validation_guard_expression = (
        "{% set validation_guard_failed = namespace(value=false) %}"
        "{% for result in results %}"
        "{% if result.status in ['error', 'fail'] and result.node.name == "
        + _sql_string(_validation_guard_model_name(spec["target"]["id"]))
        + " %}{% set validation_guard_failed.value = true %}{% endif %}"
        "{% if result.status in ['error', 'fail'] and "
        "'TYPE_MATERIALISATION_VALIDATION_FAILED' in (result.message | string) "
        "%}{% set validation_guard_failed.value = true %}{% endif %}"
        "{% endfor %}"
    )
    details_expression = (
        "{% set explicit_job_details = var(\"job_details\", none) %}"
        "{% if explicit_job_details is not none %}"
        "'{{ explicit_job_details | replace(\"'\", \"''\") }}'"
        "{% elif failed_result_count > 0 and validation_guard_failed.value %}"
        "'validation errors failed the load'"
        "{% elif failed_result_count > 0 %}"
        "'dbt run failed; inspect dbt artifacts for runtime details'"
        "{% else %}case when quarantine_counts.QUARANTINE_COUNT > 0 "
        "then 'validation errors written to quarantine output' else null end{% endif %}"
    )
    create_sql = (
        f"create table if not exists {relation} ("
        "JOB_ID varchar(64), "
        "EVENT_TYPE varchar(32), "
        "EVENT_TIMESTAMP timestamp_tz, "
        "RESULT varchar(64), "
        "DETAILS varchar(16777216), "
        "SPEC_FILE_NAME varchar(1024), "
        "GENERATED_TABLE varchar(1024), "
        "QUARANTINE_TABLE varchar(1024), "
        "LOADED_COUNT number(38, 0), "
        "QUARANTINE_COUNT number(38, 0), "
        f"AUDIT_DATA_PROCESS_KEY {GENERATED_METADATA_FIELD_TYPES['audit_data_process_key']}, "
        f"AUDIT_CREATED_DATETIME {GENERATED_METADATA_FIELD_TYPES['audit_created_datetime']}, "
        f"AUDIT_LAST_CHANGED_DATETIME {GENERATED_METADATA_FIELD_TYPES['audit_last_changed_datetime']}"
        ")"
    )
    start_sql = (
        f"insert into {relation} "
        "(JOB_ID, EVENT_TYPE, EVENT_TIMESTAMP, RESULT, DETAILS, SPEC_FILE_NAME, GENERATED_TABLE, "
        "QUARANTINE_TABLE, LOADED_COUNT, QUARANTINE_COUNT, AUDIT_DATA_PROCESS_KEY, "
        "AUDIT_CREATED_DATETIME, AUDIT_LAST_CHANGED_DATETIME) select "
        f"{_job_id_expression()}, "
        "'JOB_START', "
        "cast(current_timestamp() as timestamp_tz), "
        "null, "
        "null, "
        f"cast({_sql_string(spec_file_name)} as varchar(1024)), "
        f"cast({_sql_string(generated_table)} as varchar(1024)), "
        f"{_nullable_sql_string(quarantine_table)}, "
        "cast(null as number(38, 0)), "
        "cast(null as number(38, 0)), "
        f"cast('{{{{ var(\"audit_data_process_key\", \"manual\") }}}}' as "
        f"{GENERATED_METADATA_FIELD_TYPES['audit_data_process_key']}), "
        "cast(current_timestamp() as timestamp_tz), "
        "cast(current_timestamp() as timestamp_tz)"
    )
    end_sql = (
        f"{generated_relation_lookup}{quarantine_relation_lookup}insert into {relation} "
        "(JOB_ID, EVENT_TYPE, EVENT_TIMESTAMP, RESULT, DETAILS, SPEC_FILE_NAME, GENERATED_TABLE, "
        "QUARANTINE_TABLE, LOADED_COUNT, QUARANTINE_COUNT, AUDIT_DATA_PROCESS_KEY, "
        "AUDIT_CREATED_DATETIME, AUDIT_LAST_CHANGED_DATETIME) "
        "with "
        f"{_count_cte('tms_generated_relation', 'loaded_counts', 'loaded_count')}, "
        f"{_count_cte('tms_quarantine_relation', 'quarantine_counts', 'quarantine_count')} "
        "select "
        f"{_job_id_expression()}, "
        "'JOB_END', "
        "cast(current_timestamp() as timestamp_tz), "
        f"{result_expression}, "
        f"{failed_validation_guard_expression}{details_expression}, "
        f"cast({_sql_string(spec_file_name)} as varchar(1024)), "
        f"cast({_sql_string(generated_table)} as varchar(1024)), "
        f"{_nullable_sql_string(quarantine_table)}, "
        "loaded_counts.LOADED_COUNT, "
        "quarantine_counts.QUARANTINE_COUNT, "
        f"cast('{{{{ var(\"audit_data_process_key\", \"manual\") }}}}' as "
        f"{GENERATED_METADATA_FIELD_TYPES['audit_data_process_key']}), "
        "cast(current_timestamp() as timestamp_tz), "
        "cast(current_timestamp() as timestamp_tz) "
        "from loaded_counts cross join quarantine_counts"
    )
    bookmark_start_hooks, bookmark_end_hooks = _incremental_bookmark_hooks(spec)
    return (
        [*bookmark_start_hooks, _optional_job_hook(create_sql), _optional_job_hook(start_sql)],
        [*bookmark_end_hooks, _optional_job_hook(create_sql), _optional_job_hook(end_sql)],
    )



def _optional_job_hook(sql: str) -> str:
    return "{% if var('tms_enable_job_hooks', true) %}" + sql + "{% endif %}"
