"""Shared SQL and relation rendering helpers for generated dbt projects."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

STAGING_SCHEMA_DEFAULT = "INTERMEDIATE"

@dataclass(frozen=True)

class RelationConfig:
    database: str | None
    schema: str
    table: str



def _indent_sql(sql: str) -> str:
    return "\n".join(f"    {line}" if line.strip() else "" for line in sql.splitlines())



def _stage_reference(value: str) -> str:
    text = value[1:] if value.startswith("@") else value
    relation, separator, path = text.partition("/")
    stage_reference = _physical_name(relation)
    if separator:
        stage_reference = f"{stage_reference}/{path}"
    return f"@{stage_reference}"



def _macro_object_name(macro_name: str) -> str:
    return macro_name.rpartition(".")[2]



def _quote_identifier(value: str) -> str:
    return _physical_name(value)



def _physical_name(value: Any) -> str:
    text = str(value)
    if "{{" in text:
        return text
    return text.upper()



def _staging_schema(spec: dict[str, Any]) -> str:
    control_data = spec.get("control_data", {})
    if isinstance(control_data, dict) and control_data.get("staging_schema"):
        return _physical_name(control_data["staging_schema"])
    return STAGING_SCHEMA_DEFAULT



def _staging_schema_config_expression(spec: dict[str, Any]) -> str:
    return "var('tms_staging_schema', '" + _staging_schema(spec) + "') | upper"



def _target_schema_config_expression(spec: dict[str, Any]) -> str:
    target = spec["target"]
    return "var('target_schema', '" + _physical_name(target["schema"]) + "') | upper"



def _target_relation_config(spec: dict[str, Any]) -> RelationConfig:
    target = spec["target"]
    return RelationConfig(
        database=_physical_name(target["database"]) if target.get("database") else None,
        schema=_physical_name(target["schema"]),
        table=_physical_name(target.get("table_name", target["id"])),
    )



def _quarantine_relation_config(spec: dict[str, Any]) -> RelationConfig:
    target = _target_relation_config(spec)
    quarantine = spec.get("control_data", {}).get("quarantine", {})
    if not isinstance(quarantine, dict):
        quarantine = {}
    return RelationConfig(
        database=_physical_name(quarantine["database"]) if quarantine.get("database") else target.database,
        schema=_physical_name(quarantine["schema"]) if quarantine.get("schema") else _staging_schema(spec),
        table=_physical_name(quarantine.get("table", f"{target.table}__QUARANTINE")),
    )



def _quarantine_has_explicit_schema(spec: dict[str, Any]) -> bool:
    quarantine = spec.get("control_data", {}).get("quarantine", {})
    return isinstance(quarantine, dict) and bool(quarantine.get("schema"))



def _job_relation_config(spec: dict[str, Any]) -> RelationConfig:
    target = _target_relation_config(spec)
    job = spec.get("control_data", {}).get("job", {})
    if not isinstance(job, dict):
        job = {}
    job_schema = _physical_name(job.get("schema", "BUSINESS"))
    return RelationConfig(
        database=_physical_name(job["database"]) if job.get("database") else target.database,
        schema=f"{{{{ var('tms_job_schema', '{job_schema}') | upper }}}}",
        table=_physical_name(job.get("table", "TYPE_MATERIALISATION_JOBS")),
    )



def _relation_name(relation: RelationConfig) -> str:
    parts = []
    if relation.database:
        parts.append(relation.database)
    parts.extend([relation.schema, relation.table])
    return ".".join(parts)



def _runtime_relation_label(relation: RelationConfig, *, schema_var: str | None = None) -> str:
    database = relation.database or "{{ target.database | upper }}"
    if schema_var is None:
        schema = relation.schema
    else:
        schema = '{{ var("' + schema_var + '", "' + relation.schema + '") | upper }}'
    return ".".join([database, schema, relation.table])



def _dbt_relation_lookup(relation: RelationConfig, variable_name: str, *, schema_var: str | None = None) -> str:
    database = _sql_string(relation.database) if relation.database else "(target.database | upper)"
    if schema_var is None:
        schema = _sql_string(relation.schema)
    else:
        schema = f'(var("{schema_var}", "{relation.schema}") | upper)'
    return (
        "{% set "
        + variable_name
        + " = adapter.get_relation(database="
        + database
        + ", schema="
        + schema
        + ", identifier="
        + _sql_string(_physical_name(relation.table))
        + ") %}"
    )



def _count_expression(relation_variable_name: str) -> str:
    return (
        "{% if "
        + relation_variable_name
        + " is not none %}(select count(*) from {{ "
        + relation_variable_name
        + " }}){% else %}null{% endif %}"
    )



def _count_cte(relation_variable_name: str, cte_name: str, column_name: str) -> str:
    return (
        cte_name
        + " as (select "
        + _count_expression(relation_variable_name)
        + " as "
        + _physical_name(column_name)
        + ")"
    )



def _job_id_expression() -> str:
    return "cast('{{ invocation_id }}' as varchar(64))"



def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"



def _runtime_sql_string(value: str) -> str:
    """Quote a runtime relation while keeping embedded dbt var() calls parseable."""
    runtime_value = re.sub(
        r"\{\{\s*var\(\s*'([^']+)'\s*,\s*'([^']*)'\s*\)\s*\}\}",
        r'{{ var("\1", "\2") }}',
        value,
    )
    return _sql_string(runtime_value)



def _sql_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    string_value = str(value)
    if _is_jinja_expression(string_value):
        return f"'{string_value}'"
    return _sql_string(string_value)



def _is_jinja_expression(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith("{{") and stripped.endswith("}}")



def _nullable_sql_string(value: str | None) -> str:
    if value is None:
        return "cast(null as varchar(1024))"
    return f"cast({_sql_string(value)} as varchar(1024))"



def _sql_literal(value: Any, data_type: str) -> str:
    if value is None:
        return f"cast(null as {data_type})"
    return f"cast({_sql_string(str(value))} as {data_type})"
