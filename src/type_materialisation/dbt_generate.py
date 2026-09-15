import csv
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .custom_macros import MacroLoadError, PythonMacroResolver
from .errors import Diagnostic
from .inheritance import InheritanceError, resolve_spec
from .schema import require_yaml
from .spec import (
    BUSINESS_KEY_DATA_TYPE,
    GENERATED_METADATA_FIELD_TYPES,
    RESERVED_GENERATED_FIELDS,
    SCD2_MANUAL_FIELD_TYPES,
    SURROGATE_KEY_DATA_TYPE,
    case_key,
    fields,
    parse_sql_type,
    scd2_validity_column,
    scd2_validity_config,
    scd2_validity_data_type,
    scd2_validity_fields,
)
from .variables import resolve_python_templates

from .dbt_hooks import _tag_post_hook_lines, _write_generated_macros, _write_project_file
from .dbt_source import (
    _defaulted_csv_source_value, _source_column_name, _source_model_name,
    _source_output_columns, _write_source_model,
)
from .dbt_sql import (
    _dbt_relation_lookup, _job_id_expression, _macro_object_name, _physical_name,
    _quarantine_has_explicit_schema, _quarantine_relation_config, _quote_identifier,
    _sql_literal, _sql_scalar, _sql_string, _staging_schema_config_expression,
    _target_relation_config, _target_schema_config_expression,
)


DBT_PROJECT_NAME = "type_materialisation_generated"
DBT_PROFILE_NAME = "datahub_type_materialisation"
SCD2_START_OF_TIME = "0001-01-01T00:00:00Z"
SCD2_END_OF_TIME = "9999-12-31T23:59:59Z"
SCD2_END_OF_TIME_VALIDATION_THRESHOLD = "9999-12-30 00:00:00"
STAGING_SCHEMA_DEFAULT = "INTERMEDIATE"

NOT_IMPLEMENTED = [
    "generated Snowflake file-format objects for CSV stages",
    "Python upload of local CSV files to Snowflake stages",
    "date/timestamp format translation from Python strptime to Snowflake formats",
    "multi-error quarantine output",
]


@dataclass
class DbtGenerationResult:
    output_dir: Path
    files: list[Path] = field(default_factory=list)
    warnings: list[Diagnostic] = field(default_factory=list)
    errors: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class GenerateDbtOptions:
    spec_path: Path
    output_dir: Path
    csv_stage: str | None = None
    unit_test_csv: Path | None = None
    macro_paths: list[Path] = field(default_factory=list)
    spec: dict[str, Any] | None = None
    spec_paths: list[Path] = field(default_factory=list)
    vars: dict[str, Any] = field(default_factory=dict)




def generate_dbt_project(options: GenerateDbtOptions) -> DbtGenerationResult:
    result = DbtGenerationResult(output_dir=options.output_dir)
    if options.output_dir.exists() and any(options.output_dir.iterdir()):
        result.errors.append(
            Diagnostic(
                "output directory already exists and is not empty; choose an empty directory for dbt generation",
                str(options.output_dir),
            )
        )
        return result
    if options.spec is None:
        try:
            spec = resolve_spec(options.spec_path, spec_paths=options.spec_paths).spec
        except InheritanceError as exc:
            result.errors.append(Diagnostic(str(exc), "inheritance"))
            return result
    else:
        spec = options.spec
    spec, variable_diagnostics = resolve_python_templates(spec, variables=options.vars)
    if variable_diagnostics:
        result.errors.extend(variable_diagnostics)
        return result
    diagnostics = _unsupported_for_initial_dbt_generation(spec)
    if diagnostics:
        result.errors.extend(diagnostics)
        return result
    if _change_type(spec) in {"scd2_auto", "scd2_derived"} and not _scd2_validation_enabled(spec):
        result.warnings.append(
            Diagnostic(
                "WARNING! SCD2 validity-window validation is disabled; invalid, overlapping, or non-continuous history may be loaded",
                "$.control_data.scd.scd2_validation_enabled",
            )
        )
    if not _validation_enabled(spec):
        result.warnings.append(
            Diagnostic(
                "WARNING! General validation is disabled; validation failures will not block or quarantine rows.",
                "$.control_data.validation_enabled",
            )
        )

    macros = PythonMacroResolver(spec_path=options.spec_path, macro_paths=options.macro_paths)
    custom_macro_names = _custom_macro_names(spec)
    generated_macros: dict[str, str] = {}
    for macro_name in custom_macro_names:
        try:
            macros.require_sql_generation(macro_name)
            reference = macros.resolve_reference(macro_name)
            generated_macros[reference.macro_name] = reference.macro.generate_dbt_macro()
            if not macros.has_python_callable(macro_name):
                result.warnings.append(
                    Diagnostic(
                        "custom macro has no Python execution callable; generated dbt SQL will be used at runtime",
                        macro_name,
                    )
                )
        except MacroLoadError as exc:
            result.errors.append(Diagnostic(str(exc), macro_name))
    if result.errors:
        return result

    _ensure_dirs(options.output_dir)
    _write_project_file(options.output_dir, spec, options.spec_path.name, result)
    _write_status_file(options.output_dir, result)
    _write_source_model(spec, options, result)
    if result.errors:
        return result
    try:
        _write_final_model(spec, result)
    except ValueError as exc:
        result.errors.append(Diagnostic(str(exc), "$.target.fields"))
        return result
    if _fail_load_enabled(spec):
        _write_validation_guard_model(spec, result)
    _write_generated_macros(options.output_dir, generated_macros, spec, options.spec_path.name, result)
    if options.unit_test_csv is not None:
        try:
            _write_unit_tests(spec, options, macros, result)
        except ValueError as exc:
            result.errors.append(Diagnostic(str(exc), str(options.unit_test_csv)))
    result.warnings = _dedupe_diagnostics(result.warnings)
    return result


def _unsupported_for_initial_dbt_generation(spec: dict[str, Any]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    materialisation_type = spec.get("control_data", {}).get("materialisation_type", "table")
    source = spec.get("source", {})
    scd = _scd_config(spec)
    if materialisation_type != "table":
        diagnostics.append(
            Diagnostic("dbt generation supports materialisation_type = table", "$.control_data.materialisation_type")
        )
    if not isinstance(source, dict) or source.get("format") not in {"csv", "table"}:
        diagnostics.append(Diagnostic("dbt generation supports source.format = csv or table", "$.source.format"))
    if _business_key_required_for_generation(spec) and not _business_key_field_ids(spec):
        diagnostics.append(
            Diagnostic(
                "`business_key.fields` must contain at least one field id",
                "$.control_data.business_key.fields",
            )
        )
    for index, field_id in enumerate(_business_key_field_ids(spec)):
        if _surrogate_key_enabled(spec) and case_key(field_id) == case_key(_surrogate_key_column(spec)):
            diagnostics.append(
                Diagnostic(
                    "business key field must not reference generated surrogate key",
                    f"$.control_data.business_key.fields[{index}]",
                )
            )
            continue
        try:
            _field_by_id(spec, field_id)
        except ValueError:
            diagnostics.append(
                Diagnostic(
                    "business key field does not exist in target.fields",
                    f"$.control_data.business_key.fields[{index}]",
                )
            )
    if _change_type(spec) == "scd2_auto":
        if "insert_time" not in scd:
            diagnostics.append(
                Diagnostic(
                    "`scd.insert_time` is required when `change_type` is scd2_auto",
                    "$.control_data.scd.insert_time",
                )
            )
        if "delete_detection" in scd:
            diagnostics.append(
                Diagnostic(
                    "`delete_detection.mode = field` is only valid when `change_type` is scd1",
                    "$.control_data.scd.delete_detection.mode",
                )
            )
    if _change_type(spec) == "scd2_derived":
        if "valid_from_datetime" in scd:
            diagnostics.append(
                Diagnostic(
                    "`scd.valid_from_datetime` is not valid for `scd2_derived`; declare `target.fields.valid_from_datetime` instead",
                    "$.control_data.scd.valid_from_datetime",
                )
            )
        if "delete_detection" in scd:
            diagnostics.append(
                Diagnostic(
                    "`delete_detection.mode = field` is only valid when `change_type` is scd1",
                    "$.control_data.scd.delete_detection.mode",
                )
            )
    if _change_type(spec) == "scd1" and "scd2_auto_from_sot" in scd:
        diagnostics.append(
            Diagnostic(
                "`scd2_auto_from_sot` is only valid when `change_type` is scd2_auto",
                "$.control_data.scd.scd2_auto_from_sot",
            )
        )
    if _change_type(spec) not in {"scd2_auto", "scd2_derived"} and "scd2_validation" in scd:
        diagnostics.append(
            Diagnostic(
                "`scd2_validation` is only valid when `change_type` is scd2_auto or scd2_derived",
                "$.control_data.scd.scd2_validation",
            )
        )
    if _change_type(spec) not in {"scd2_auto", "scd2_derived"} and "scd2_validation_enabled" in scd:
        diagnostics.append(
            Diagnostic(
                "`scd2_validation_enabled` is only valid when `change_type` is scd2_auto or scd2_derived",
                "$.control_data.scd.scd2_validation_enabled",
            )
        )
    if _change_type(spec) in {"scd1", "scd2_auto", "scd2_derived"} and "update_mode" in scd:
        diagnostics.append(
            Diagnostic(
                "`update_mode` is only valid when `change_type` is scd2_manual",
                "$.control_data.scd.update_mode",
            )
        )
    if _change_type(spec) == "scd2_manual":
        invalid_keys = set(scd) - {"update_mode", "update_key", "validity"}
        if invalid_keys:
            diagnostics.append(
                Diagnostic(
                    "`scd2_manual` supports only `scd.update_mode`, `scd.update_key`, and `scd.validity`",
                    "$.control_data.scd",
                )
            )
        if "update_key" in scd and _scd2_manual_update_mode(spec) != "upsert":
            diagnostics.append(
                Diagnostic(
                    "`scd.update_key` is only valid when `scd.update_mode` is upsert",
                    "$.control_data.scd.update_key",
                )
            )
        if _scd2_manual_update_mode(spec) == "upsert" and not _business_key_field_ids(spec):
            diagnostics.append(
                Diagnostic(
                    "`business_key.fields` is required when `scd.update_mode` is upsert",
                    "$.control_data.business_key.fields",
                )
            )
        for index, field_id in enumerate(_scd2_manual_update_key_field_ids(spec)):
            try:
                _field_by_id(spec, field_id)
            except ValueError:
                diagnostics.append(
                    Diagnostic(
                        "update key field does not exist in target.fields",
                        f"$.control_data.scd.update_key.fields[{index}]",
                    )
                )
    return diagnostics


def _ensure_dirs(output_dir: Path) -> None:
    (output_dir / "models" / "generated").mkdir(parents=True, exist_ok=True)
    (output_dir / "macros" / "generated").mkdir(parents=True, exist_ok=True)
    (output_dir / "macros" / "reference").mkdir(parents=True, exist_ok=True)
    (output_dir / "seeds").mkdir(parents=True, exist_ok=True)




def _write_status_file(output_dir: Path, result: DbtGenerationResult) -> None:
    lines = [
        "# Not Yet Implemented",
        "",
        "This generated dbt project covers the first implementation slice only.",
        "",
    ]
    lines.extend(f"- {item}" for item in NOT_IMPLEMENTED)
    _write(output_dir / "NOT_IMPLEMENTED.md", "\n".join(lines) + "\n", result)














def _scd2_derived_source_helper_lines(spec: dict[str, Any]) -> list[str]:
    """SCD2-derived valid-from values are projected through target.fields."""
    del spec
    return []


def _input_fields(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Target fields plus virtual source-backed SCD validity boundaries."""
    return [*fields(spec), *scd2_validity_fields(spec)]


def _write_final_model(spec: dict[str, Any], result: DbtGenerationResult) -> None:
    target = spec["target"]
    table_name = target.get("table_name", target["id"])
    materialized = spec.get("control_data", {}).get("materialisation_type", "table")
    source_model = _source_model_name(target["id"])
    quarantine_enabled = _quarantine_enabled(spec)
    fail_load_enabled = _fail_load_enabled(spec)
    field_select_lines = []
    for field in _input_fields(spec):
        if _scd2_derived_declared_metadata(field, spec):
            continue
        expression = _field_expression(field)
        data_type = field["data_type"]
        field_select_lines.append(f"    cast({expression} as {data_type}) as {_quote_identifier(field['id'])}")
    business_key_select_lines = _business_key_select_lines(spec)
    surrogate_key_select_lines = _surrogate_key_select_lines(spec)
    business_data_hash_select_lines = _business_data_hash_select_lines(spec)
    audit_select_lines = _audit_select_lines()
    extra_config_lines: list[str] = []
    if _scd2_uses_target_merge(spec):
        materialized = "incremental"
        extra_config_lines = [
            "    incremental_strategy='delete+insert',",
            f"    unique_key={_scd2_incremental_unique_key(spec)},",
            "    on_schema_change='fail',",
        ]
    elif _change_type(spec) == "scd2_manual":
        if _truncate_before_load_enabled(spec):
            materialized = "table"
        elif _scd2_manual_uses_business_key_upsert(spec):
            materialized = "incremental"
            extra_config_lines = [
                "    incremental_strategy='delete+insert',",
                f"    unique_key={_scd2_manual_incremental_unique_key(spec)},",
                "    on_schema_change='fail',",
            ]
        else:
            materialized = "incremental"
            extra_config_lines = [
                "    incremental_strategy='append',",
                "    on_schema_change='fail',",
            ]
    extra_config_lines.extend(_tag_post_hook_lines(spec))
    config_lines = _model_config_lines(
        materialized=materialized,
        schema=_target_schema_config_expression(spec),
        alias=table_name,
        database=target.get("database"),
        extra_config_lines=extra_config_lines,
        schema_is_expression=True,
    )
    if _generated_scd2_enabled(spec):
        body_lines = _scd2_final_body_lines(
            spec,
            source_model,
            field_select_lines,
            surrogate_key_select_lines,
            audit_select_lines,
            quarantine_enabled=quarantine_enabled,
            fail_load_enabled=fail_load_enabled,
        )
    elif _change_type(spec) == "scd2_manual":
        select_lines = _scd2_manual_select_lines(
            spec,
            surrogate_key_select_lines,
            business_key_select_lines,
            business_data_hash_select_lines,
            audit_select_lines,
        )
        delete_filter_lines = _scd1_delete_filter_lines(spec)
        if quarantine_enabled:
            body_lines = [
                "with source_rows as (",
                f"    select * from {{{{ ref('{source_model}') }}}}",
                "),",
                *_validation_rows_cte(spec, trailing_comma=True),
                *_valid_rows_cte(),
                "",
                "select",
                ",\n".join(select_lines),
                "from valid_rows",
                *delete_filter_lines,
                "",
            ]
        elif fail_load_enabled:
            validation_guard_model = _validation_guard_model_name(target["id"])
            body_lines = [
                "with source_rows as (",
                f"    select * from {{{{ ref('{source_model}') }}}}",
                "),",
                *_validation_rows_cte(spec, trailing_comma=True),
                "valid_rows as (",
                "    select validation_rows.*",
                "    from validation_rows",
                f"    cross join {{{{ ref('{validation_guard_model}') }}}} as validation_guard",
                "    where validation_rows.FAILURE_DETAILS is null",
                "      and validation_guard.VALIDATION_FAILURE_GUARD = 0",
                ")",
                "",
                "select",
                ",\n".join(select_lines),
                "from valid_rows",
                *delete_filter_lines,
                "",
            ]
        else:
            body_lines = [
                "select",
                ",\n".join(select_lines),
                f"from {{{{ ref('{source_model}') }}}}",
                *delete_filter_lines,
                "",
            ]
    elif quarantine_enabled:
        select_lines = [
            *field_select_lines,
            *surrogate_key_select_lines,
            *business_key_select_lines,
            *business_data_hash_select_lines,
            *audit_select_lines,
        ]
        delete_filter_lines = _scd1_delete_filter_lines(spec)
        body_lines = [
            "with source_rows as (",
            f"    select * from {{{{ ref('{source_model}') }}}}",
            "),",
            *_validation_rows_cte(spec, trailing_comma=True),
            *_valid_rows_cte(),
            "",
            "select",
            ",\n".join(select_lines),
            "from valid_rows",
            *delete_filter_lines,
            "",
        ]
    elif fail_load_enabled:
        select_lines = [
            *field_select_lines,
            *surrogate_key_select_lines,
            *business_key_select_lines,
            *business_data_hash_select_lines,
            *audit_select_lines,
        ]
        delete_filter_lines = _scd1_delete_filter_lines(spec)
        validation_guard_model = _validation_guard_model_name(target["id"])
        body_lines = [
            "with source_rows as (",
            f"    select * from {{{{ ref('{source_model}') }}}}",
            "),",
            *_validation_rows_cte(spec, trailing_comma=True),
            "valid_rows as (",
            "    select validation_rows.*",
            "    from validation_rows",
            f"    cross join {{{{ ref('{validation_guard_model}') }}}} as validation_guard",
            "    where validation_rows.FAILURE_DETAILS is null",
            "      and validation_guard.VALIDATION_FAILURE_GUARD = 0",
            ")",
            "",
            "select",
            ",\n".join(select_lines),
            "from valid_rows",
            *delete_filter_lines,
            "",
        ]
    else:
        select_lines = [
            *field_select_lines,
            *surrogate_key_select_lines,
            *business_key_select_lines,
            *business_data_hash_select_lines,
            *audit_select_lines,
        ]
        delete_filter_lines = _scd1_delete_filter_lines(spec)
        body_lines = [
            "select",
            ",\n".join(select_lines),
            f"from {{{{ ref('{source_model}') }}}}",
            *delete_filter_lines,
            "",
        ]
    runtime_log_lines = _scd2_duplicate_hash_runtime_log_lines(
        spec,
        source_model,
        field_select_lines,
        surrogate_key_select_lines,
        audit_select_lines,
        quarantine_enabled=quarantine_enabled,
        fail_load_enabled=fail_load_enabled,
    )
    truncate_guard_lines = _truncate_guard_lines(spec)
    validation_failure_lines = _fail_load_validation_failure_lines(spec) if fail_load_enabled else []
    sql = "\n".join(
        [
            "{{",
            "  config(",
            *config_lines,
            "  )",
            "}}",
            "",
            *truncate_guard_lines,
            *runtime_log_lines,
            *validation_failure_lines,
            *body_lines,
        ]
    )
    _write(result.output_dir / "models" / "generated" / f"{target['id']}.sql", sql, result)
    if quarantine_enabled:
        _write_quarantine_model(spec, result)


def _audit_select_lines() -> list[str]:
    return [
        f"    cast(current_timestamp() as {GENERATED_METADATA_FIELD_TYPES['audit_created_datetime']}) "
        "as AUDIT_CREATED_DATETIME",
        f"    cast(current_timestamp() as {GENERATED_METADATA_FIELD_TYPES['audit_last_changed_datetime']}) "
        "as AUDIT_LAST_CHANGED_DATETIME",
        "    cast('{{ var(\"audit_data_process_key\", \"manual\") }}' "
        f"as {GENERATED_METADATA_FIELD_TYPES['audit_data_process_key']}) as AUDIT_DATA_PROCESS_KEY",
    ]

def _field_select_line(field: dict[str, Any]) -> str:
    return f"    cast({_field_expression(field)} as {field['data_type']}) as {_quote_identifier(field['id'])}"


def _scd2_derived_declared_metadata(field: dict[str, Any], spec: dict[str, Any]) -> bool:
    return _change_type(spec) in {"scd2_derived", "scd2_manual"} and case_key(str(field.get("id", ""))) in {
        case_key(_scd2_valid_from_column(spec)), case_key(_scd2_valid_to_column(spec))
    }


def _scd2_manual_select_lines(
    spec: dict[str, Any],
    surrogate_key_select_lines: list[str],
    business_key_select_lines: list[str],
    business_data_hash_select_lines: list[str],
    audit_select_lines: list[str],
) -> list[str]:
    business_fields, source_audit_fields, scd2_state_fields = _scd2_declared_field_groups(spec)
    declared_scd2_by_key = {case_key(str(field["id"])): field for field in scd2_state_fields}
    scd2_state_select_lines = [
        _field_select_line(declared_scd2_by_key[case_key(column_id)])
        for column_id in _scd2_state_column_ids(spec)
        if case_key(column_id) in declared_scd2_by_key
    ]
    # New validity contracts keep boundaries out of target.fields; project their
    # source-backed virtual fields in the generated SCD2 state position.
    scd2_state_select_lines.extend(
        _field_select_line(field) for field in scd2_validity_fields(spec)
    )
    return [
        *surrogate_key_select_lines,
        *business_key_select_lines,
        *[_field_select_line(field) for field in business_fields],
        *scd2_state_select_lines,
        *business_data_hash_select_lines,
        *[_field_select_line(field) for field in source_audit_fields],
        *audit_select_lines,
    ]


def _scd2_final_body_lines(
    spec: dict[str, Any],
    source_model: str,
    field_select_lines: list[str],
    surrogate_key_select_lines: list[str],
    audit_select_lines: list[str],
    *,
    quarantine_enabled: bool,
    fail_load_enabled: bool,
) -> list[str]:
    if _scd2_uses_target_merge(spec):
        return _scd2_target_merge_body_lines(
            spec,
            source_model,
            field_select_lines,
            surrogate_key_select_lines,
            audit_select_lines,
            quarantine_enabled=quarantine_enabled,
            fail_load_enabled=fail_load_enabled,
        )

    output_lines = _scd2_output_lines(spec)
    return [
        *_scd2_valid_rows_lines(spec, source_model, quarantine_enabled, fail_load_enabled),
        "typed_rows as (",
        "    select",
        ",\n".join(
            [
                *field_select_lines,
                *surrogate_key_select_lines,
                *_business_key_select_lines(spec),
                f"    {_valid_from_datetime_expression(spec)} as TMS_VALID_FROM_DATETIME_CANDIDATE",
                f"    {_business_data_hash_expression(spec)} as BUSINESS_DATA_HASH",
            ]
        ),
        "    from valid_rows",
        "),",
        "valid_from_rows as (",
        "    select",
        "        *,",
        "        case",
        f"            when row_number() over ({_scd2_window_clause(spec, 'TMS_VALID_FROM_DATETIME_CANDIDATE')}) = 1",
        f"            then cast({_sql_string(_scd2_start_of_time(spec))} as {_scd2_validity_data_type(spec)})",
        "            else TMS_VALID_FROM_DATETIME_CANDIDATE",
        f"        end as {_scd2_valid_from_column(spec)}",
        "    from typed_rows",
        "),",
        "windowed_rows as (",
        "    select",
        "        *,",
        f"        {_valid_to_datetime_expression(spec)} as {_scd2_valid_to_column(spec)}",
        "    from valid_from_rows",
        "),",
        "flagged_rows as (",
        "    select",
        "        *,",
        "        case",
        f"            when {_scd2_valid_to_column(spec)} = {_scd2_end_of_time_expression(spec)}",
        "            then 'Y'",
        "            else 'N'",
        "        end as IS_CURRENT_FLAG,",
        f"        {_is_deleted_flag_expression(spec)} as IS_DELETED_FLAG",
        "    from windowed_rows",
        ")," if _scd2_validation_enabled(spec) else ")",
        *_scd2_validation_cte_lines(spec),
        "",
        "select",
        ",\n".join(output_lines),
        "from flagged_rows",
        *_scd2_validation_guard_join_lines(spec, quarantine_enabled=quarantine_enabled),
        "",
    ]


def _scd2_duplicate_hash_runtime_log_lines(
    spec: dict[str, Any],
    source_model: str,
    field_select_lines: list[str],
    surrogate_key_select_lines: list[str],
    audit_select_lines: list[str],
    *,
    quarantine_enabled: bool,
    fail_load_enabled: bool,
) -> list[str]:
    if not _scd2_uses_target_merge(spec):
        return []
    change_row_columns = _scd2_change_row_columns(spec)
    return [
        "{% if execute and is_incremental() and var('tms_log_scd2_duplicate_hash_metrics', false) and not var('tms_unit_test', false) %}",
        "{% set tms_scd2_duplicate_hash_log_sql %}",
        *_scd2_valid_rows_lines(spec, source_model, quarantine_enabled, fail_load_enabled),
        "typed_rows as (",
        "    select",
        ",\n".join(
            [
                *field_select_lines,
                *surrogate_key_select_lines,
                *_business_key_select_lines(spec),
                f"    {_valid_from_datetime_expression(spec)} as TMS_VALID_FROM_DATETIME_CANDIDATE",
                f"    {_business_data_hash_expression(spec)} as BUSINESS_DATA_HASH",
                f"    {_is_deleted_flag_expression(spec)} as TMS_IS_DELETED_FLAG_CANDIDATE",
                f"    cast(null as {_scd2_timestamp_data_type(spec)}) as TMS_EXISTING_VALID_TO_DATETIME",
                "    cast(null as varchar(1)) as TMS_EXISTING_IS_CURRENT_FLAG",
                "    'N' as TMS_IS_EXISTING_TARGET_ROW",
                *audit_select_lines,
            ]
        ),
        "    from valid_rows",
        "),",
        "incoming_key_rows as (",
        "    select distinct",
        ",\n".join(_business_key_cte_select_lines(spec, "        ")),
        "    from typed_rows",
        "),",
        *_scd2_runtime_existing_target_rows_lines(spec),
        "current_target_rows as (",
        "    select *",
        "    from existing_target_rows",
        "    where IS_CURRENT_FLAG = 'Y'",
        "),",
        "current_duplicate_rows as (",
        "    select typed_rows.*",
        "    from typed_rows",
        "    where exists (",
        "        select 1",
        "        from current_target_rows as current_target",
        f"        where {_business_key_join_condition(spec, 'typed_rows', 'current_target')}",
        "          and current_target.BUSINESS_DATA_HASH = typed_rows.BUSINESS_DATA_HASH",
        "          and coalesce(current_target.IS_DELETED_FLAG, 'N') = typed_rows.TMS_IS_DELETED_FLAG_CANDIDATE",
        f"          and typed_rows.TMS_VALID_FROM_DATETIME_CANDIDATE >= current_target.{_scd2_valid_from_column(spec)}",
        "    )",
        "),",
        "source_change_rows as (",
        "    select *",
        "    from typed_rows",
        "    where not exists (",
        "        select 1",
        "        from current_target_rows as current_target",
        f"        where {_business_key_join_condition(spec, 'typed_rows', 'current_target')}",
        "          and current_target.BUSINESS_DATA_HASH = typed_rows.BUSINESS_DATA_HASH",
        "          and coalesce(current_target.IS_DELETED_FLAG, 'N') = typed_rows.TMS_IS_DELETED_FLAG_CANDIDATE",
        f"          and typed_rows.TMS_VALID_FROM_DATETIME_CANDIDATE >= current_target.{_scd2_valid_from_column(spec)}",
        "    )",
        "),",
        *_scd2_empty_delete_rows_lines(spec, audit_select_lines),
        "change_rows as (",
        "    select",
        ",\n".join(f"        {column}" for column in change_row_columns),
        "    from source_change_rows",
        "    union all",
        "    select",
        ",\n".join(f"        {column}" for column in change_row_columns),
        "    from synthetic_delete_rows",
        "),",
        "affected_key_rows as (",
        "    select distinct",
        ",\n".join(_business_key_cte_select_lines(spec, "        ")),
        "    from change_rows",
        "),",
        "affected_existing_rows as (",
        "    select",
        ",\n".join(
            [
                *[
                    f"        existing_target.{_quote_identifier(field['id'])} as {_quote_identifier(field['id'])}"
                    for field in fields(spec)
                ],
                *_surrogate_key_existing_target_select_lines(spec),
                *_business_key_existing_target_select_lines(spec),
                f"        existing_target.{_scd2_valid_from_column(spec)} as TMS_VALID_FROM_DATETIME_CANDIDATE",
                "        existing_target.BUSINESS_DATA_HASH as BUSINESS_DATA_HASH",
                "        existing_target.IS_DELETED_FLAG as TMS_IS_DELETED_FLAG_CANDIDATE",
                f"        existing_target.{_scd2_valid_to_column(spec)} as TMS_EXISTING_VALID_TO_DATETIME",
                "        existing_target.IS_CURRENT_FLAG as TMS_EXISTING_IS_CURRENT_FLAG",
                "        'Y' as TMS_IS_EXISTING_TARGET_ROW",
                "        existing_target.AUDIT_DATA_PROCESS_KEY as AUDIT_DATA_PROCESS_KEY",
                "        existing_target.AUDIT_CREATED_DATETIME as AUDIT_CREATED_DATETIME",
                "        existing_target.AUDIT_LAST_CHANGED_DATETIME as AUDIT_LAST_CHANGED_DATETIME",
            ]
        ),
        "    from existing_target_rows as existing_target",
        "    where exists (",
        "        select 1",
        "        from affected_key_rows as affected_key",
        f"        where {_business_key_join_condition(spec, 'existing_target', 'affected_key')}",
        "    )",
        "),",
        "version_row_candidates as (",
        "    select",
        ",\n".join(f"        {column}" for column in change_row_columns),
        "    from affected_existing_rows",
        "    union all",
        "    select",
        ",\n".join(f"        {column}" for column in change_row_columns),
        "    from change_rows",
        "),",
        "version_rows as (",
        "    select",
        ",\n".join(f"        {column}" for column in change_row_columns),
        "    from (",
        "        select",
        "            *,",
        "            row_number() over (",
        f"                partition by {_scd2_version_row_key_columns(spec)}",
        f"                order by {_scd2_version_row_order_by(spec)}",
        "            ) as TMS_VERSION_ROW_NUMBER",
        "        from version_row_candidates",
        "    )",
        "    where TMS_VERSION_ROW_NUMBER = 1",
        "),",
        "duplicate_boundary_rows as (",
        "    select",
        "        *,",
        f"        lag(BUSINESS_DATA_HASH) over ({_scd2_window_clause(spec, 'TMS_VALID_FROM_DATETIME_CANDIDATE')}) as TMS_PREVIOUS_BUSINESS_DATA_HASH,",
        f"        lag(TMS_IS_DELETED_FLAG_CANDIDATE) over ({_scd2_window_clause(spec, 'TMS_VALID_FROM_DATETIME_CANDIDATE')}) as TMS_PREVIOUS_IS_DELETED_FLAG,",
        f"        lag(TMS_IS_EXISTING_TARGET_ROW) over ({_scd2_window_clause(spec, 'TMS_VALID_FROM_DATETIME_CANDIDATE')}) as TMS_PREVIOUS_IS_EXISTING_TARGET_ROW,",
        f"        lag(BUSINESS_DATA_HASH, 2) over ({_scd2_window_clause(spec, 'TMS_VALID_FROM_DATETIME_CANDIDATE')}) as TMS_PREVIOUS_2_BUSINESS_DATA_HASH,",
        f"        lag(TMS_IS_DELETED_FLAG_CANDIDATE, 2) over ({_scd2_window_clause(spec, 'TMS_VALID_FROM_DATETIME_CANDIDATE')}) as TMS_PREVIOUS_2_IS_DELETED_FLAG,",
        f"        lead(BUSINESS_DATA_HASH) over ({_scd2_window_clause(spec, 'TMS_VALID_FROM_DATETIME_CANDIDATE')}) as TMS_NEXT_BUSINESS_DATA_HASH,",
        (
            f"        lead(TMS_IS_DELETED_FLAG_CANDIDATE) over "
            f"({_scd2_window_clause(spec, 'TMS_VALID_FROM_DATETIME_CANDIDATE')}) as TMS_NEXT_IS_DELETED_FLAG"
        ),
        "    from version_rows",
        "),",
        "current_duplicate_counts as (",
        "    select count(*) as CURRENT_DUPLICATE_SKIP_COUNT",
        "    from current_duplicate_rows",
        "),",
        "duplicate_boundary_counts as (",
        "    select",
        f"        coalesce(sum(case when TMS_IS_EXISTING_TARGET_ROW = 'N' and ({_scd2_previous_duplicate_condition()} or {_scd2_next_duplicate_condition()}) then 1 else 0 end), 0) as HISTORICAL_DUPLICATE_SKIP_COUNT,",
        f"        coalesce(sum(case when ((TMS_IS_EXISTING_TARGET_ROW = 'N' and {_scd2_previous_duplicate_condition()}) or (TMS_IS_EXISTING_TARGET_ROW = 'Y' and TMS_PREVIOUS_IS_EXISTING_TARGET_ROW = 'N' and {_scd2_previous_duplicate_condition()} and not ({_scd2_previous_2_duplicate_condition()}))) then 1 else 0 end), 0) as HISTORICAL_BOUNDARY_UPDATE_COUNT,",
        (
            "        coalesce(sum(case when TMS_IS_EXISTING_TARGET_ROW = 'Y' "
            "and TMS_PREVIOUS_IS_EXISTING_TARGET_ROW = 'Y' "
            f"and {_scd2_previous_duplicate_condition()} then 1 else 0 end), 0) "
            "as CONTIGUOUS_DUPLICATE_HASH_COUNT"
        ),
        "    from duplicate_boundary_rows",
        ")",
        "select",
        "    current_duplicate_counts.CURRENT_DUPLICATE_SKIP_COUNT,",
        "    duplicate_boundary_counts.HISTORICAL_DUPLICATE_SKIP_COUNT,",
        "    duplicate_boundary_counts.HISTORICAL_BOUNDARY_UPDATE_COUNT,",
        "    duplicate_boundary_counts.CONTIGUOUS_DUPLICATE_HASH_COUNT",
        "from current_duplicate_counts",
        "cross join duplicate_boundary_counts",
        "{% endset %}",
        "{% set tms_scd2_duplicate_hash_log_result = run_query(tms_scd2_duplicate_hash_log_sql) %}",
        "{% if tms_scd2_duplicate_hash_log_result is not none and (tms_scd2_duplicate_hash_log_result.rows | length) > 0 %}",
        "{% set tms_current_duplicate_skip_count = tms_scd2_duplicate_hash_log_result.columns[0].values()[0] | int %}",
        "{% set tms_historical_duplicate_skip_count = tms_scd2_duplicate_hash_log_result.columns[1].values()[0] | int %}",
        "{% set tms_historical_boundary_update_count = tms_scd2_duplicate_hash_log_result.columns[2].values()[0] | int %}",
        "{% set tms_contiguous_duplicate_hash_count = tms_scd2_duplicate_hash_log_result.columns[3].values()[0] | int %}",
        "{% if tms_current_duplicate_skip_count > 0 %}{{ log('SCD2 duplicate hash handling: current duplicate rows skipped=' ~ tms_current_duplicate_skip_count, info=true) }}{% endif %}",
        "{% if tms_historical_boundary_update_count > 0 %}{{ log('SCD2 duplicate hash handling: historical duplicate boundaries updated=' ~ tms_historical_boundary_update_count, info=true) }}{% endif %}",
        "{% if tms_contiguous_duplicate_hash_count > 0 %}{{ log('SCD2 duplicate hash handling: contiguous duplicate hash windows detected=' ~ tms_contiguous_duplicate_hash_count, info=true) }}{% endif %}",
        "{% endif %}",
        "{% endif %}",
        "",
    ]


def _scd2_runtime_existing_target_rows_lines(spec: dict[str, Any]) -> list[str]:
    columns = _scd2_target_column_types(spec)
    return [
        "existing_target_rows as (",
        "    select",
        ",\n".join(f"        {_quote_identifier(column)}" for column, _ in columns),
        "    from {{ this }} as existing_target",
        "    where exists (",
        "        select 1",
        "        from incoming_key_rows as incoming_key",
        f"        where {_business_key_join_condition(spec, 'existing_target', 'incoming_key')}",
        "    )",
        "),",
    ]


def _scd2_target_merge_body_lines(
    spec: dict[str, Any],
    source_model: str,
    field_select_lines: list[str],
    surrogate_key_select_lines: list[str],
    audit_select_lines: list[str],
    *,
    quarantine_enabled: bool,
    fail_load_enabled: bool,
) -> list[str]:
    output_lines = _scd2_output_lines(spec)
    change_row_columns = _scd2_change_row_columns(spec)
    source_change_not_exists_lines = _scd2_source_change_not_exists_lines(spec)
    window_and_flag_lines = _scd2_window_and_flag_lines(spec, "deduplicated_version_rows", include_validation=False)
    if not _scd2_validation_enabled(spec):
        window_and_flag_lines[-1] = ")"
    return [
        *_scd2_valid_rows_lines(spec, source_model, quarantine_enabled, fail_load_enabled),
        "typed_rows as (",
        "    select",
        ",\n".join(
            [
                *field_select_lines,
                *surrogate_key_select_lines,
                *_business_key_select_lines(spec),
                f"    {_valid_from_datetime_expression(spec)} as TMS_VALID_FROM_DATETIME_CANDIDATE",
                f"    {_business_data_hash_expression(spec)} as BUSINESS_DATA_HASH",
                f"    {_is_deleted_flag_expression(spec)} as TMS_IS_DELETED_FLAG_CANDIDATE",
                f"    cast(null as {_scd2_timestamp_data_type(spec)}) as TMS_EXISTING_VALID_TO_DATETIME",
                "    cast(null as varchar(1)) as TMS_EXISTING_IS_CURRENT_FLAG",
                "    'N' as TMS_IS_EXISTING_TARGET_ROW",
                *audit_select_lines,
            ]
        ),
        "    from valid_rows",
        "),",
        "incoming_key_rows as (",
        "    select distinct",
        ",\n".join(_business_key_cte_select_lines(spec, "        ")),
        "    from typed_rows",
        "),",
        *_scd2_existing_target_rows_lines(spec),
        "current_target_rows as (",
        "    select *",
        "    from existing_target_rows",
        "    where IS_CURRENT_FLAG = 'Y'",
        "),",
        "source_change_rows as (",
        "    select *",
        "    from typed_rows",
        *source_change_not_exists_lines,
        "),",
        *_scd2_empty_delete_rows_lines(spec, audit_select_lines),
        "change_rows as (",
        "    select",
        ",\n".join(f"        {column}" for column in change_row_columns),
        "    from source_change_rows",
        "    union all",
        "    select",
        ",\n".join(f"        {column}" for column in change_row_columns),
        "    from synthetic_delete_rows",
        "),",
        "affected_key_rows as (",
        "    select distinct",
        ",\n".join(_business_key_cte_select_lines(spec, "        ")),
        "    from change_rows",
        "),",
        "affected_existing_rows as (",
        "    select",
        ",\n".join(
            [
                *[
                    f"        existing_target.{_quote_identifier(field['id'])} as {_quote_identifier(field['id'])}"
                    for field in fields(spec)
                ],
                *_surrogate_key_existing_target_select_lines(spec),
                *_business_key_existing_target_select_lines(spec),
                f"        existing_target.{_scd2_valid_from_column(spec)} as TMS_VALID_FROM_DATETIME_CANDIDATE",
                "        existing_target.BUSINESS_DATA_HASH as BUSINESS_DATA_HASH",
                "        existing_target.IS_DELETED_FLAG as TMS_IS_DELETED_FLAG_CANDIDATE",
                f"        existing_target.{_scd2_valid_to_column(spec)} as TMS_EXISTING_VALID_TO_DATETIME",
                "        existing_target.IS_CURRENT_FLAG as TMS_EXISTING_IS_CURRENT_FLAG",
                "        'Y' as TMS_IS_EXISTING_TARGET_ROW",
                "        existing_target.AUDIT_DATA_PROCESS_KEY as AUDIT_DATA_PROCESS_KEY",
                "        existing_target.AUDIT_CREATED_DATETIME as AUDIT_CREATED_DATETIME",
                "        existing_target.AUDIT_LAST_CHANGED_DATETIME as AUDIT_LAST_CHANGED_DATETIME",
            ]
        ),
        "    from existing_target_rows as existing_target",
        "    where exists (",
        "        select 1",
        "        from affected_key_rows as affected_key",
        f"        where {_business_key_join_condition(spec, 'existing_target', 'affected_key')}",
        "    )",
        "),",
        "version_row_candidates as (",
        "    select",
        ",\n".join(f"        {column}" for column in change_row_columns),
        "    from affected_existing_rows",
        "    union all",
        "    select",
        ",\n".join(f"        {column}" for column in change_row_columns),
        "    from change_rows",
        "),",
        "version_rows as (",
        "    select",
        ",\n".join(f"        {column}" for column in change_row_columns),
        "    from (",
        "        select",
        "            *,",
        "            row_number() over (",
        f"                partition by {_scd2_version_row_key_columns(spec)}",
        f"                order by {_scd2_version_row_order_by(spec)}",
        "            ) as TMS_VERSION_ROW_NUMBER",
        "        from version_row_candidates",
        "    )",
        "    where TMS_VERSION_ROW_NUMBER = 1",
        "),",
        *_scd2_duplicate_boundary_lines(spec),
        *window_and_flag_lines,
        *_scd2_post_load_validation_rows_lines(spec),
        *_scd2_validation_cte_lines(spec, input_cte="post_load_validation_rows"),
        "",
        "select",
        ",\n".join(output_lines),
        "from flagged_rows",
        *_scd2_validation_guard_join_lines(spec, quarantine_enabled=quarantine_enabled),
        "",
    ]


def _scd2_source_change_not_exists_lines(spec: dict[str, Any]) -> list[str]:
    if _change_type(spec) == "scd2_derived":
        return [
            "    where not exists (",
            "        select 1",
            "        from existing_target_rows as existing_target",
            f"        where {_business_key_join_condition(spec, 'typed_rows', 'existing_target')}",
            f"          and existing_target.{_scd2_valid_from_column(spec)} = typed_rows.TMS_VALID_FROM_DATETIME_CANDIDATE",
            "          and existing_target.BUSINESS_DATA_HASH = typed_rows.BUSINESS_DATA_HASH",
            "          and coalesce(existing_target.IS_DELETED_FLAG, 'N') = typed_rows.TMS_IS_DELETED_FLAG_CANDIDATE",
            "    )",
            "      and not exists (",
            "        select 1",
            "        from existing_target_rows as current_target",
            f"        where {_business_key_join_condition(spec, 'typed_rows', 'current_target')}",
            "          and current_target.IS_CURRENT_FLAG = 'Y'",
            "          and current_target.BUSINESS_DATA_HASH = typed_rows.BUSINESS_DATA_HASH",
            "          and coalesce(current_target.IS_DELETED_FLAG, 'N') = typed_rows.TMS_IS_DELETED_FLAG_CANDIDATE",
            f"          and typed_rows.TMS_VALID_FROM_DATETIME_CANDIDATE >= current_target.{_scd2_valid_from_column(spec)}",
            "    )",
        ]
    return [
        "    where not exists (",
        "        select 1",
        "        from current_target_rows as current_target",
        f"        where {_business_key_join_condition(spec, 'typed_rows', 'current_target')}",
        "          and current_target.BUSINESS_DATA_HASH = typed_rows.BUSINESS_DATA_HASH",
        "          and coalesce(current_target.IS_DELETED_FLAG, 'N') = typed_rows.TMS_IS_DELETED_FLAG_CANDIDATE",
        f"          and typed_rows.TMS_VALID_FROM_DATETIME_CANDIDATE >= current_target.{_scd2_valid_from_column(spec)}",
        "    )",
    ]


def _scd2_valid_rows_lines(
    spec: dict[str, Any],
    source_model: str,
    quarantine_enabled: bool,
    fail_load_enabled: bool,
) -> list[str]:
    if quarantine_enabled:
        return [
            "with source_rows as (",
            f"    select * from {{{{ ref('{source_model}') }}}}",
            "),",
            *_validation_rows_cte(spec, trailing_comma=True),
            *_valid_rows_cte(),
            ",",
        ]
    if fail_load_enabled:
        validation_guard_model = _validation_guard_model_name(spec["target"]["id"])
        return [
            "with source_rows as (",
            f"    select * from {{{{ ref('{source_model}') }}}}",
            "),",
            *_validation_rows_cte(spec, trailing_comma=True),
            "valid_rows as (",
            "    select validation_rows.*",
            "    from validation_rows",
            f"    cross join {{{{ ref('{validation_guard_model}') }}}} as validation_guard",
            "    where validation_rows.FAILURE_DETAILS is null",
            "      and validation_guard.VALIDATION_FAILURE_GUARD = 0",
            "),",
        ]
    return [
        "with source_rows as (",
        f"    select * from {{{{ ref('{source_model}') }}}}",
        "),",
        "valid_rows as (",
        "    select * from source_rows",
        "),",
    ]


def _scd2_uses_target_merge(spec: dict[str, Any]) -> bool:
    return _generated_scd2_enabled(spec) and not _truncate_before_load_enabled(spec)


def _generated_scd2_enabled(spec: dict[str, Any]) -> bool:
    return _change_type(spec) in {"scd2_auto", "scd2_derived"}


def _scd2_manual_uses_business_key_upsert(spec: dict[str, Any]) -> bool:
    return _change_type(spec) == "scd2_manual" and _scd2_manual_update_mode(spec) == "upsert"


def _scd2_manual_update_mode(spec: dict[str, Any]) -> str:
    return str(_scd_config(spec).get("update_mode", "append_only"))


def _scd2_manual_update_key_field_ids(spec: dict[str, Any]) -> list[str]:
    if _scd2_manual_update_mode(spec) != "upsert":
        return []
    update_key = _scd_config(spec).get("update_key", {})
    if isinstance(update_key, dict):
        configured_fields = update_key.get("fields", [])
        if isinstance(configured_fields, list) and configured_fields:
            return [str(value) for value in configured_fields if isinstance(value, str)]
    return ["valid_from_datetime"]


def _scd2_manual_incremental_unique_key(spec: dict[str, Any]) -> str:
    columns = _scd2_manual_incremental_unique_key_columns(spec)
    return "[" + ", ".join(_sql_string(column) for column in columns) + "]"


def _scd2_manual_incremental_unique_key_columns(spec: dict[str, Any]) -> list[str]:
    return [
        *_business_key_unique_key_columns(spec),
        *(_physical_name(field_id) for field_id in _scd2_manual_update_key_field_ids(spec)),
    ]


def _truncate_guard_lines(spec: dict[str, Any]) -> list[str]:
    if not _truncate_before_load_enabled(spec):
        return []
    return [
        "{% set tms_allow_truncate = var('allow_truncate', false) %}",
        "{% if tms_allow_truncate != true and (tms_allow_truncate | string | lower) != 'true' %}",
        '  {{ exceptions.raise_compiler_error("truncate_before_load requires dbt var `allow_truncate: true`") }}',
        "{% endif %}",
        "",
    ]


def _scd2_incremental_unique_key(spec: dict[str, Any]) -> str:
    return "[" + ", ".join(_sql_string(column) for column in _business_key_unique_key_columns(spec)) + "]"


SCD2_STATE_COLUMN_IDS = ["IS_CURRENT_FLAG", "IS_DELETED_FLAG"]
SCD2_SURROGATE_KEY_NAMESPACE = "5aa788a5-783e-5ce6-b4aa-438de8fc6971"
SOURCE_AUDIT_COLUMN_IDS = [
    "AUDIT_CREATED_SOURCE",
    "AUDIT_LAST_CHANGED_SOURCE",
]
SYSTEM_AUDIT_COLUMN_IDS = [
    "AUDIT_CREATED_DATETIME",
    "AUDIT_LAST_CHANGED_DATETIME",
    "AUDIT_DATA_PROCESS_KEY",
]


def _scd2_valid_from_column(spec: dict[str, Any]) -> str:
    return _physical_name(scd2_validity_column(spec, "valid_from"))


def _scd2_valid_to_column(spec: dict[str, Any]) -> str:
    return _physical_name(scd2_validity_column(spec, "valid_to"))


def _scd2_state_column_ids(spec: dict[str, Any]) -> list[str]:
    return [*SCD2_STATE_COLUMN_IDS, _scd2_valid_from_column(spec), _scd2_valid_to_column(spec)]


def _scd2_declared_field_groups(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    scd2_state_keys = {case_key(column_id) for column_id in _scd2_state_column_ids(spec)}
    source_audit_keys = {case_key(column_id) for column_id in SOURCE_AUDIT_COLUMN_IDS}
    business_fields: list[dict[str, Any]] = []
    source_audit_fields: list[dict[str, Any]] = []
    scd2_state_fields: list[dict[str, Any]] = []

    for field in fields(spec):
        field_key = case_key(str(field["id"]))
        if field_key in scd2_state_keys:
            scd2_state_fields.append(field)
        elif field_key in source_audit_keys:
            source_audit_fields.append(field)
        else:
            business_fields.append(field)

    return business_fields, source_audit_fields, scd2_state_fields


def _scd2_declared_column_types(fields_to_emit: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(str(field["id"]), str(field["data_type"])) for field in fields_to_emit]


def _scd2_state_column_types(spec: dict[str, Any], declared_scd2_fields: list[dict[str, Any]]) -> list[tuple[str, str]]:
    declared_by_key = {case_key(str(field["id"])): field for field in declared_scd2_fields}
    columns: list[tuple[str, str]] = []
    for column_id in _scd2_state_column_ids(spec):
        declared_field = declared_by_key.get(case_key(column_id))
        if declared_field is not None:
            columns.append((str(declared_field["id"]), str(declared_field["data_type"])))
            continue
        generated_key = case_key(column_id.lower())
        metadata_name = {
            case_key("IS_CURRENT_FLAG"): "is_current_flag",
            case_key("IS_DELETED_FLAG"): "is_deleted_flag",
        }.get(generated_key)
        data_type = _scd2_validity_data_type(spec) if column_id in {_scd2_valid_from_column(spec), _scd2_valid_to_column(spec)} else GENERATED_METADATA_FIELD_TYPES[str(metadata_name)]
        columns.append((column_id, data_type))
    return columns


def _scd2_system_audit_column_types() -> list[tuple[str, str]]:
    return [
        ("AUDIT_CREATED_DATETIME", GENERATED_METADATA_FIELD_TYPES["audit_created_datetime"]),
        ("AUDIT_LAST_CHANGED_DATETIME", GENERATED_METADATA_FIELD_TYPES["audit_last_changed_datetime"]),
        ("AUDIT_DATA_PROCESS_KEY", GENERATED_METADATA_FIELD_TYPES["audit_data_process_key"]),
    ]


def _scd2_output_lines(spec: dict[str, Any]) -> list[str]:
    business_fields, source_audit_fields, _ = _scd2_declared_field_groups(spec)
    lines: list[str] = []
    lines.extend(_surrogate_key_output_lines(spec))
    lines.extend(_business_key_output_lines(spec))
    lines.extend(f"    {_quote_identifier(field['id'])}" for field in business_fields)
    lines.extend(f"    {column_id}" for column_id in _scd2_state_column_ids(spec))
    lines.append("    BUSINESS_DATA_HASH")
    lines.extend(f"    {_quote_identifier(field['id'])}" for field in source_audit_fields)
    lines.extend(
        [
            "    AUDIT_CREATED_DATETIME",
            "\n".join(
                [
                    "    case",
                    "        when TMS_IS_EXISTING_TARGET_ROW = 'Y' then AUDIT_LAST_CHANGED_DATETIME",
                    (
                        "        else cast(current_timestamp() as "
                        f"{GENERATED_METADATA_FIELD_TYPES['audit_last_changed_datetime']})"
                    ),
                    "    end as AUDIT_LAST_CHANGED_DATETIME",
                ]
            ),
            "\n".join(
                [
                    "    case",
                    "        when TMS_IS_EXISTING_TARGET_ROW = 'Y' then AUDIT_DATA_PROCESS_KEY",
                    (
                        "        else cast('{{ var(\"audit_data_process_key\", \"manual\") }}' as "
                        f"{GENERATED_METADATA_FIELD_TYPES['audit_data_process_key']})"
                    ),
                    "    end as AUDIT_DATA_PROCESS_KEY",
                ]
            ),
        ]
    )
    return lines


def _scd2_target_column_types(spec: dict[str, Any]) -> list[tuple[str, str]]:
    business_fields, source_audit_fields, scd2_state_fields = _scd2_declared_field_groups(spec)
    columns: list[tuple[str, str]] = []
    if _surrogate_key_enabled(spec):
        columns.append((_surrogate_key_column(spec), SURROGATE_KEY_DATA_TYPE))
    if _business_key_enabled(spec):
        columns.append((_business_key_column(spec), BUSINESS_KEY_DATA_TYPE))
    columns.extend(_scd2_declared_column_types(business_fields))
    columns.extend(_scd2_state_column_types(spec, scd2_state_fields))
    columns.append(("BUSINESS_DATA_HASH", GENERATED_METADATA_FIELD_TYPES["business_data_hash"]))
    columns.extend(_scd2_declared_column_types(source_audit_fields))
    columns.extend(_scd2_system_audit_column_types())
    return columns


def _scd2_existing_target_rows_lines(spec: dict[str, Any]) -> list[str]:
    columns = _scd2_target_column_types(spec)
    scope_to_incoming_keys = _change_type(spec) == "scd2_derived" and _scd_validation_scope(spec) == "affected_window"
    incremental_from_lines = ["    from {{ this }}"]
    if scope_to_incoming_keys:
        incremental_from_lines = [
            "    from {{ this }} as existing_target",
            "    where exists (",
            "        select 1",
            "        from incoming_key_rows as incoming_key",
            f"        where {_business_key_join_condition(spec, 'existing_target', 'incoming_key')}",
            "    )",
        ]
    return [
        "{% if is_incremental() %}",
        "existing_target_rows as (",
        "    select",
        ",\n".join(f"        {_quote_identifier(column)}" for column, _ in columns),
        *incremental_from_lines,
        "),",
        "{% else %}",
        "existing_target_rows as (",
        "    select",
        ",\n".join(f"        cast(null as {data_type}) as {_quote_identifier(column)}" for column, data_type in columns),
        "    where 1 = 0",
        "),",
        "{% endif %}",
    ]


def _scd2_change_row_columns(spec: dict[str, Any]) -> list[str]:
    return [
        *[_quote_identifier(field["id"]) for field in fields(spec) if not _scd2_derived_declared_metadata(field, spec)],
        *_surrogate_key_change_row_columns(spec),
        *_business_key_change_row_columns(spec),
        "TMS_VALID_FROM_DATETIME_CANDIDATE",
        "BUSINESS_DATA_HASH",
        "TMS_IS_DELETED_FLAG_CANDIDATE",
        "TMS_EXISTING_VALID_TO_DATETIME",
        "TMS_EXISTING_IS_CURRENT_FLAG",
        "TMS_IS_EXISTING_TARGET_ROW",
        "AUDIT_DATA_PROCESS_KEY",
        "AUDIT_CREATED_DATETIME",
        "AUDIT_LAST_CHANGED_DATETIME",
    ]


def _scd2_empty_delete_rows_lines(spec: dict[str, Any], audit_select_lines: list[str]) -> list[str]:
    return [
        "synthetic_delete_rows as (",
        "    select",
        ",\n".join(
            [
                *[
                    f"        cast(null as {field['data_type']}) as {_quote_identifier(field['id'])}"
                    for field in fields(spec)
                ],
                *_surrogate_key_null_select_lines(spec),
                *_business_key_null_select_lines(spec),
                f"        cast(null as {_scd2_timestamp_data_type(spec)}) as TMS_VALID_FROM_DATETIME_CANDIDATE",
                "        cast(null as varchar(64)) as BUSINESS_DATA_HASH",
                "        cast(null as varchar(1)) as TMS_IS_DELETED_FLAG_CANDIDATE",
                f"        cast(null as {_scd2_timestamp_data_type(spec)}) as TMS_EXISTING_VALID_TO_DATETIME",
                "        cast(null as varchar(1)) as TMS_EXISTING_IS_CURRENT_FLAG",
                "        cast(null as varchar(1)) as TMS_IS_EXISTING_TARGET_ROW",
                *audit_select_lines,
            ]
        ),
        "    where 1 = 0",
        "),",
    ]


def _scd2_duplicate_boundary_lines(spec: dict[str, Any]) -> list[str]:
    return [
        "duplicate_boundary_rows as (",
        "    select",
        "        *,",
        f"        lag(BUSINESS_DATA_HASH) over ({_scd2_window_clause(spec, 'TMS_VALID_FROM_DATETIME_CANDIDATE')}) as TMS_PREVIOUS_BUSINESS_DATA_HASH,",
        f"        lag(TMS_IS_DELETED_FLAG_CANDIDATE) over ({_scd2_window_clause(spec, 'TMS_VALID_FROM_DATETIME_CANDIDATE')}) as TMS_PREVIOUS_IS_DELETED_FLAG,",
        f"        lag(TMS_IS_EXISTING_TARGET_ROW) over ({_scd2_window_clause(spec, 'TMS_VALID_FROM_DATETIME_CANDIDATE')}) as TMS_PREVIOUS_IS_EXISTING_TARGET_ROW,",
        f"        lag(BUSINESS_DATA_HASH, 2) over ({_scd2_window_clause(spec, 'TMS_VALID_FROM_DATETIME_CANDIDATE')}) as TMS_PREVIOUS_2_BUSINESS_DATA_HASH,",
        f"        lag(TMS_IS_DELETED_FLAG_CANDIDATE, 2) over ({_scd2_window_clause(spec, 'TMS_VALID_FROM_DATETIME_CANDIDATE')}) as TMS_PREVIOUS_2_IS_DELETED_FLAG,",
        f"        lead(BUSINESS_DATA_HASH) over ({_scd2_window_clause(spec, 'TMS_VALID_FROM_DATETIME_CANDIDATE')}) as TMS_NEXT_BUSINESS_DATA_HASH,",
        (
            f"        lead(TMS_IS_DELETED_FLAG_CANDIDATE) over "
            f"({_scd2_window_clause(spec, 'TMS_VALID_FROM_DATETIME_CANDIDATE')}) as TMS_NEXT_IS_DELETED_FLAG"
        ),
        "    from version_rows",
        "),",
        "deduplicated_version_rows as (",
        "    select",
        ",\n".join(f"        {column}" for column in _scd2_change_row_columns(spec)),
        "    from duplicate_boundary_rows",
        f"    where {_scd2_duplicate_boundary_keep_condition(spec)}",
        "),",
    ]


def _scd2_duplicate_boundary_keep_condition(spec: dict[str, Any]) -> str:
    previous_matches = _scd2_previous_duplicate_condition()
    return (
        "not ("
        f"(TMS_IS_EXISTING_TARGET_ROW = 'N' and {previous_matches}) "
        "or "
        "(TMS_IS_EXISTING_TARGET_ROW = 'Y' "
        "and TMS_PREVIOUS_IS_EXISTING_TARGET_ROW = 'N' "
        f"and {previous_matches} "
        f"and not ({_scd2_previous_2_duplicate_condition()}))"
        ")"
    )


def _scd2_previous_duplicate_condition() -> str:
    return (
        "TMS_PREVIOUS_BUSINESS_DATA_HASH is not null "
        "and BUSINESS_DATA_HASH = TMS_PREVIOUS_BUSINESS_DATA_HASH "
        "and coalesce(TMS_IS_DELETED_FLAG_CANDIDATE, 'N') = coalesce(TMS_PREVIOUS_IS_DELETED_FLAG, 'N')"
    )


def _scd2_previous_2_duplicate_condition() -> str:
    return (
        "TMS_PREVIOUS_2_BUSINESS_DATA_HASH is not null "
        "and BUSINESS_DATA_HASH = TMS_PREVIOUS_2_BUSINESS_DATA_HASH "
        "and coalesce(TMS_IS_DELETED_FLAG_CANDIDATE, 'N') = coalesce(TMS_PREVIOUS_2_IS_DELETED_FLAG, 'N')"
    )


def _scd2_next_duplicate_condition() -> str:
    return (
        "TMS_NEXT_BUSINESS_DATA_HASH is not null "
        "and BUSINESS_DATA_HASH = TMS_NEXT_BUSINESS_DATA_HASH "
        "and coalesce(TMS_IS_DELETED_FLAG_CANDIDATE, 'N') = coalesce(TMS_NEXT_IS_DELETED_FLAG, 'N')"
    )


def _business_key_join_condition(spec: dict[str, Any], left_alias: str, right_alias: str) -> str:
    return " and ".join(
        f"{left_alias}.{column} = {right_alias}.{column}"
        for column in _business_key_partition_columns(spec)
    )


def _scd2_version_row_key_columns(spec: dict[str, Any]) -> str:
    return ", ".join([*_business_key_partition_columns(spec), "TMS_VALID_FROM_DATETIME_CANDIDATE"])


def _scd2_version_row_order_by(spec: dict[str, Any]) -> str:
    order_expressions = ["case when TMS_IS_EXISTING_TARGET_ROW = 'N' then 0 else 1 end"]
    if _change_type(spec) == "scd2_derived":
        order_expressions.extend(_scd2_deduplicate_order_by_expressions(spec))
    return ", ".join(order_expressions)


def _scd2_deduplicate_order_by_expressions(spec: dict[str, Any]) -> list[str]:
    deduplicate = _scd_config(spec).get("deduplicate", {})
    if not isinstance(deduplicate, dict):
        return []
    expressions: list[str] = []
    valid_from_reference = _source_column_name(_field_by_id(spec, "valid_from_datetime"))
    for order_by in deduplicate.get("order_by", []):
        if not isinstance(order_by, dict):
            continue
        column = order_by.get("column")
        if not isinstance(column, str):
            continue
        direction = str(order_by.get("direction", "desc")).lower()
        nulls = str(order_by.get("nulls", "last")).lower()
        if direction not in {"asc", "desc"}:
            direction = "desc"
        if nulls not in {"first", "last"}:
            nulls = "last"
        if (
            isinstance(valid_from_reference, str)
            and case_key(column) == case_key(valid_from_reference)
        ):
            column = "TMS_VALID_FROM_DATETIME_CANDIDATE"
        expressions.append(f"{_quote_identifier(column)} {direction} nulls {nulls}")
    return expressions


def _scd2_window_and_flag_lines(
    spec: dict[str, Any],
    input_cte: str,
    *,
    include_validation: bool = True,
) -> list[str]:
    if _scd2_auto_from_sot(spec):
        valid_from_expression_lines = [
            "        case",
            f"            when row_number() over ({_scd2_window_clause(spec, 'TMS_VALID_FROM_DATETIME_CANDIDATE')}) = 1",
            f"            then cast({_sql_string(_scd2_start_of_time(spec))} as {_scd2_validity_data_type(spec)})",
            "            else TMS_VALID_FROM_DATETIME_CANDIDATE",
            f"        end as {_scd2_valid_from_column(spec)}",
        ]
    else:
        valid_from_expression_lines = [
            f"        TMS_VALID_FROM_DATETIME_CANDIDATE as {_scd2_valid_from_column(spec)}",
        ]
    lines = [
        "valid_from_rows as (",
        "    select",
        "        *,",
        *valid_from_expression_lines,
        f"    from {input_cte}",
        "),",
        "windowed_rows as (",
        "    select",
        "        *,",
        f"        {_valid_to_datetime_expression(spec)} as {_scd2_valid_to_column(spec)}",
        "    from valid_from_rows",
        "),",
        "flagged_rows as (",
        "    select",
        "        *,",
        "        case",
        f"            when {_scd2_valid_to_column(spec)} = {_scd2_end_of_time_expression(spec)}",
        "            then 'Y'",
        "            else 'N'",
        "        end as IS_CURRENT_FLAG,",
        "        TMS_IS_DELETED_FLAG_CANDIDATE as IS_DELETED_FLAG",
        "    from windowed_rows",
        "),",
    ]
    if include_validation:
        validation_lines = _scd2_validation_cte_lines(spec)
        if validation_lines:
            lines.extend(validation_lines)
        else:
            lines[-1] = ")"
    return lines


def _scd2_post_load_validation_rows_lines(spec: dict[str, Any]) -> list[str]:
    if not _scd2_validation_enabled(spec):
        return []
    columns = [*_business_key_partition_columns(spec), _scd2_valid_from_column(spec), _scd2_valid_to_column(spec)]
    if _change_type(spec) == "scd2_derived" and _scd_validation_scope(spec) == "affected_window":
        return [
            "post_load_validation_rows as (",
            "    select",
            ",\n".join(f"        {_quote_identifier(column)}" for column in columns),
            "    from flagged_rows",
            "),",
        ]

    # Incremental SCD2 emits affected keys only. Validation still covers the
    # post-load target as a whole: rewritten keys plus untouched existing keys.
    return [
        "post_load_validation_rows as (",
        "    select",
        ",\n".join(f"        {_quote_identifier(column)}" for column in columns),
        "    from flagged_rows",
        "    union all",
        "    select",
        ",\n".join(f"        existing_target.{_quote_identifier(column)}" for column in columns),
        "    from existing_target_rows as existing_target",
        "    where not exists (",
        "        select 1",
        "        from affected_key_rows as affected_key",
        f"        where {_business_key_join_condition(spec, 'existing_target', 'affected_key')}",
        "    )",
        "),",
    ]


def _scd2_validation_cte_lines(spec: dict[str, Any], *, input_cte: str = "flagged_rows") -> list[str]:
    if not _scd2_validation_enabled(spec):
        return []
    # SCD2 validity windows are inclusive: VALID_TO_DATETIME is still live.
    # Adjacent continuous rows must therefore start one timestamp tick after the previous row ends.
    conditions = [
        f"{_scd2_valid_from_column(spec)} is null",
        f"{_scd2_valid_to_column(spec)} is null",
        f"{_scd2_valid_to_column(spec)} <= {_scd2_valid_from_column(spec)}",
        f"TMS_NEXT_VALID_FROM_DATETIME <= {_scd2_valid_to_column(spec)}",
    ]
    if _scd2_validation_mode(spec) == "continuous":
        conditions.extend(
            [
                (
                    "TMS_NEXT_VALID_FROM_DATETIME is not null "
                    f"and TMS_NEXT_VALID_FROM_DATETIME <> dateadd({_scd2_validity_offset(spec)['unit']}, {-_scd2_validity_offset(spec)['value']}, {_scd2_valid_to_column(spec)})"
                ),
                (
                    "TMS_NEXT_VALID_FROM_DATETIME is null "
                    f"and {_scd2_valid_to_column(spec)} < cast({_sql_string(_scd2_end_of_time_validation_threshold(spec))} as {_scd2_validity_data_type(spec)})"
                ),
            ]
        )
    condition_sql = "\n       or ".join(f"({condition})" for condition in conditions)
    return [
        "scd2_validation_windowed_rows as (",
        "    select",
        "        *,",
        (
            f"        lead({_scd2_valid_from_column(spec)}) over ({_scd2_window_clause(spec, _scd2_valid_from_column(spec))}) "
            "as TMS_NEXT_VALID_FROM_DATETIME"
        ),
        f"    from {input_cte}",
        "),",
        "scd2_invalid_validity_rows as (",
        "    select *",
        "    from scd2_validation_windowed_rows",
        (
            "    -- Accept near-end-of-time values so timezone normalisation of "
            "9999-12-31 timestamps does not falsely reject open-ended rows."
        ),
        f"    where {condition_sql}",
        "),",
        "scd2_validation_failure_count as (",
        "    select count(*) as SCD2_VALIDATION_FAILURE_COUNT",
        "    from scd2_invalid_validity_rows",
        "),",
        "scd2_validation_guard as (",
        "    select",
        "        case",
        "            when SCD2_VALIDATION_FAILURE_COUNT = 0 then 0",
        (
            "            -- Deliberately fail the model rather than allowing a validation failure "
            "to replace the target with zero rows."
        ),
        "            else 1 / case when SCD2_VALIDATION_FAILURE_COUNT > 0 then 0 else 1 end",
        "        end as SCD2_VALIDATION_GUARD",
        "    from scd2_validation_failure_count",
        ")",
    ]


def _scd2_validation_guard_join_lines(spec: dict[str, Any], *, quarantine_enabled: bool) -> list[str]:
    if not _scd2_validation_enabled(spec):
        return []
    if quarantine_enabled:
        # A malformed window invalidates the full business-key history, not only
        # the individual version row. Do not emit partial history for that key.
        return [
            "where not exists (",
            "    select 1",
            "    from scd2_invalid_validity_rows as invalid_row",
            f"    where {_business_key_join_condition(spec, 'flagged_rows', 'invalid_row')}",
            ")",
        ]
    return [
        "cross join scd2_validation_guard",
        "where scd2_validation_guard.SCD2_VALIDATION_GUARD = 0",
    ]


def _write_validation_guard_model(spec: dict[str, Any], result: DbtGenerationResult) -> None:
    target = spec["target"]
    relation = _target_relation_config(spec)
    source_model = _source_model_name(target["id"])
    model_name = _validation_guard_model_name(target["id"])
    alias = f"{relation.table}__VALIDATION_GUARD"
    config_lines = _model_config_lines(
        materialized="table",
        database=relation.database,
        schema=_staging_schema_config_expression(spec),
        alias=alias,
        extra_config_lines=[
            "    pre_hook='drop table if exists {{ this }}',",
        ],
        schema_is_expression=True,
    )
    sql = "\n".join(
        [
            "{{",
            "  config(",
            *config_lines,
            "  )",
            "}}",
            "",
            "with source_rows as (",
            f"    select * from {{{{ ref('{source_model}') }}}}",
            "),",
            *_validation_rows_cte(spec, trailing_comma=True),
            "failed_validation_rows as (",
            "    select *",
            "    from validation_rows",
            "    where FAILURE_DETAILS is not null",
            "),",
            "failure_count as (",
            "    select",
            "        count(*) as FAILURE_COUNT,",
            "        min(FAILURE_DETAILS) as FAILURE_DETAILS",
            "    from failed_validation_rows",
            ")",
            "",
            "select",
            "    case when FAILURE_COUNT = 0 then 0 else 1 end as VALIDATION_FAILURE_GUARD,",
            "    FAILURE_COUNT as VALIDATION_FAILURE_COUNT,",
            "    FAILURE_DETAILS as VALIDATION_FAILURE_DETAILS",
            "from failure_count",
            "",
        ]
    )
    _write(result.output_dir / "models" / "generated" / f"{model_name}.sql", sql, result)


def _fail_load_validation_failure_lines(spec: dict[str, Any]) -> list[str]:
    target = spec["target"]
    validation_guard_model = _validation_guard_model_name(target["id"])
    return [
        "{% if execute and not var('tms_unit_test', false) %}",
        "{% set tms_validation_failure_sql %}",
        "select",
        "    VALIDATION_FAILURE_COUNT,",
        "    VALIDATION_FAILURE_DETAILS",
        f"from {{{{ ref('{validation_guard_model}') }}}}",
        "where VALIDATION_FAILURE_GUARD <> 0",
        "limit 1",
        "{% endset %}",
        "{% set tms_validation_failure_result = run_query(tms_validation_failure_sql) %}",
        "{% if tms_validation_failure_result is not none and (tms_validation_failure_result.rows | length) > 0 %}",
        "{% set tms_validation_failure_count = tms_validation_failure_result.columns[0].values()[0] | int %}",
        "{% set tms_validation_failure_details = tms_validation_failure_result.columns[1].values()[0] %}",
        (
            "{{ exceptions.raise_compiler_error("
            "'TYPE_MATERIALISATION_VALIDATION_FAILED: ' "
            "~ tms_validation_failure_count "
            "~ ' validation row(s) failed. First failure: ' "
            "~ tms_validation_failure_details) }}"
        ),
        "{% endif %}",
        "{% endif %}",
        "",
    ]


def _model_config_lines(
    *,
    materialized: str,
    schema: str,
    alias: str,
    database: str | None,
    extra_config_lines: list[str] | None = None,
    schema_is_expression: bool = False,
) -> list[str]:
    schema_config = schema if schema_is_expression else f"'{_physical_name(schema)}'"
    config_lines = [
        f"    materialized='{materialized}',",
        f"    schema={schema_config},",
        f"    alias='{_physical_name(alias)}',",
    ]
    if database:
        config_lines.insert(1, f"    database='{_physical_name(database)}',")
    if extra_config_lines:
        config_lines.extend(extra_config_lines)
    return config_lines














def _write_quarantine_model(spec: dict[str, Any], result: DbtGenerationResult) -> None:
    target = spec["target"]
    quarantine = _quarantine_relation_config(spec)
    model_name = _quarantine_model_name(target["id"])
    source_model = _source_model_name(target["id"])
    source_columns = _source_output_columns(spec)
    select_lines = [
        "    cast(current_timestamp() as timestamp_ntz) as LOADED_AT",
        f"    {_job_id_expression()} as JOB_ID",
        "    FAILURE_DETAILS",
    ]
    select_lines.extend(f"    {_quote_identifier(column)}" for column in source_columns)
    config_lines = _model_config_lines(
        materialized="incremental",
        database=quarantine.database,
        schema=(
            _physical_name(quarantine.schema)
            if _quarantine_has_explicit_schema(spec)
            else _staging_schema_config_expression(spec)
        ),
        alias=quarantine.table,
        extra_config_lines=[
            "    incremental_strategy='append',",
            "    on_schema_change='append_new_columns',",
        ],
        schema_is_expression=not _quarantine_has_explicit_schema(spec),
    )
    sql = "\n".join(
        [
            "{{",
            "  config(",
            *config_lines,
            "  )",
            "}}",
            "",
            "with source_rows as (",
            f"    select * from {{{{ ref('{source_model}') }}}}",
            "),",
            *_validation_rows_cte(spec, trailing_comma=False),
            "",
            "select",
            ",\n".join(select_lines),
            "from validation_rows",
            "where FAILURE_DETAILS is not null",
            "",
        ]
    )
    _write(result.output_dir / "models" / "generated" / f"{model_name}.sql", sql, result)








def _write_unit_tests(
    spec: dict[str, Any],
    options: GenerateDbtOptions,
    macros: PythonMacroResolver,
    result: DbtGenerationResult,
) -> None:
    yaml = require_yaml()
    source_rows, expected_rows, warnings = _unit_test_rows(spec, options.unit_test_csv, macros)
    result.warnings.extend(warnings)
    target = spec["target"]
    source_fixture_sql = _unit_test_fixture_sql(source_rows, _source_fixture_column_types(spec))
    given = [
        {
            "input": f"ref('{_source_model_name(target['id'])}')",
            "format": "sql",
            "rows": source_fixture_sql,
        }
    ]
    if _fail_load_enabled(spec):
        given.append(
            {
                "input": f"ref('{_validation_guard_model_name(target['id'])}')",
                "format": "sql",
                "rows": _unit_test_validation_guard_fixture_sql(),
            }
        )
    unit_test = {
        "name": f"{target['id']}_sample_source",
        "model": target["id"],
        "given": given,
        "expect": {
            "rows": expected_rows,
        },
        "overrides": _unit_test_overrides(scd2_non_incremental=_scd2_uses_target_merge(spec)),
    }
    unit_tests = [unit_test]
    if _quarantine_enabled(spec):
        unit_tests.append(
            {
                "name": f"{_quarantine_model_name(target['id'])}_sample_source",
                "model": _quarantine_model_name(target["id"]),
                "given": [
                    {
                        "input": f"ref('{_source_model_name(target['id'])}')",
                        "format": "sql",
                        "rows": source_fixture_sql,
                    }
                ],
                "expect": {
                    "rows": [],
                },
                "overrides": _unit_test_overrides(),
            }
        )
    data = {"unit_tests": unit_tests}
    content = yaml.safe_dump(data, sort_keys=False)
    _write(result.output_dir / "models" / "generated" / f"{target['id']}_unit_tests.yml", content, result)


def _unit_test_validation_guard_fixture_sql() -> str:
    return "\n".join(
        [
            "select",
            "    cast(0 as number) as VALIDATION_FAILURE_GUARD,",
            "    cast(0 as number) as VALIDATION_FAILURE_COUNT,",
            "    cast(null as varchar) as VALIDATION_FAILURE_DETAILS",
        ]
    )


def _unit_test_overrides(*, scd2_non_incremental: bool = False) -> dict[str, dict[str, bool]]:
    overrides = {"vars": {"tms_unit_test": True}}
    if scd2_non_incremental:
        overrides["macros"] = {"is_incremental": False}
    return overrides


def _unit_test_fixture_sql(rows: list[dict[str, Any]], column_types: dict[str, str]) -> str:
    if not rows:
        select_lines = [
            f"    cast(null as {data_type}) as {_quote_identifier(column)}"
            for column, data_type in column_types.items()
        ]
        return "\n".join(["select", ",\n".join(select_lines), "where 1 = 0"])

    selects = []
    for row in rows:
        select_lines = []
        for column, data_type in column_types.items():
            value = row.get(column)
            select_lines.append(f"    {_sql_literal(value, data_type)} as {_quote_identifier(column)}")
        selects.append("\n".join(["select", ",\n".join(select_lines)]))
    return "\nunion all\n".join(selects)


def _source_fixture_column_types(spec: dict[str, Any]) -> dict[str, str]:
    return {
        column: "varchar"
        for column in _source_output_columns(spec)
    }


def _unit_test_rows(
    spec: dict[str, Any],
    csv_path: Path,
    macros: PythonMacroResolver,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Diagnostic]]:
    warnings: list[Diagnostic] = []
    source = spec["source"]
    target_fields = fields(spec)
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter=source.get("delimiter", ","), quotechar=source.get("quotechar", '"'))
        header = next(reader) if source.get("header") else None
        source_rows: list[dict[str, Any]] = []
        expected_rows: list[dict[str, Any]] = []
        transformed_rows: list[dict[str, Any]] = []
        for row in reader:
            source_row: dict[str, Any] = {}
            expected_row: dict[str, Any] = {}
            transformed_values: dict[str, Any] = {}
            for field in target_fields:
                column = _source_column_name(field)
                value = _extract_csv_value(field, row, header)
                source_row[column] = value
                expected_value = _locally_transformable_value(field, value, macros, warnings)
                if expected_value is not _SKIP_EXPECTED:
                    transformed_values[case_key(field["id"])] = expected_value
                    expected_row[_physical_name(field["id"])] = expected_value
            if _business_key_enabled(spec):
                business_key_value = _unit_test_business_key(spec, transformed_values, warnings)
                if business_key_value is not _SKIP_EXPECTED:
                    expected_row[_business_key_column(spec)] = business_key_value
            if _business_data_hash_enabled(spec):
                hash_value = _unit_test_business_data_hash(spec, transformed_values, warnings)
                if hash_value is not _SKIP_EXPECTED:
                    expected_row["BUSINESS_DATA_HASH"] = hash_value
            source_rows.append(source_row)
            expected_rows.append(expected_row)
            transformed_rows.append(transformed_values)
    if _generated_scd2_enabled(spec):
        _apply_unit_test_scd2_validity(spec, expected_rows, transformed_rows, warnings)
    return source_rows, expected_rows, warnings


_SKIP_EXPECTED = object()


def _unit_test_business_key(
    spec: dict[str, Any],
    transformed_values: dict[str, Any],
    warnings: list[Diagnostic],
) -> Any:
    value = _unit_test_joined_field_values(
        spec,
        _business_key_field_ids(spec),
        "|",
        transformed_values,
        warnings,
        value_name=_business_key_column(spec),
    )
    if value is _SKIP_EXPECTED:
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _extract_csv_value(field: dict[str, Any], row: list[str], header: list[str] | None) -> str | None:
    source = field.get("source", {})
    if "fixed_value" in source:
        value = source["fixed_value"]
        return None if value is None else str(value)
    pos = source.get("pos")
    if isinstance(pos, int):
        return row[pos] if pos < len(row) else None
    column = source.get("column")
    if isinstance(column, str) and header is not None and column in header:
        index = header.index(column)
        value = row[index] if index < len(row) else None
    else:
        value = None
    return _defaulted_csv_source_value(source, value)


def _locally_transformable_value(
    field: dict[str, Any],
    value: Any,
    macros: PythonMacroResolver,
    warnings: list[Diagnostic],
) -> Any:
    current = value
    for transform in field.get("transforms", []):
        transform_type = transform.get("type")
        if transform_type == "trim":
            current = None if current is None else str(current).strip()
        elif transform_type == "round":
            continue
        elif transform_type == "custom":
            macro_name = transform["macro"]
            if not macros.has_python_callable(macro_name):
                warnings.append(
                    Diagnostic(
                        "field omitted from unit-test expectation because custom transform has no Python execution callable",
                        macro_name,
                    )
                )
                return _SKIP_EXPECTED
            current = macros.call(macro_name, value=current, field=field, rule=transform)
        else:
            warnings.append(
                Diagnostic(
                    f"field omitted from unit-test expectation because transform `{transform_type}` is not implemented locally",
                    field["id"],
                )
            )
            return _SKIP_EXPECTED
    return current


def _unit_test_business_data_hash(
    spec: dict[str, Any],
    transformed_values: dict[str, Any],
    warnings: list[Diagnostic],
) -> Any:
    return_hash_value = _unit_test_joined_field_values(
        spec,
        [str(field["id"]) for field in _business_data_hash_fields(spec)],
        "|",
        transformed_values,
        warnings,
        value_name="BUSINESS_DATA_HASH",
    )
    if return_hash_value is _SKIP_EXPECTED:
        return return_hash_value
    return hashlib.sha256(return_hash_value.encode("utf-8")).hexdigest()


def _unit_test_joined_field_values(
    spec: dict[str, Any],
    field_ids: list[str],
    separator: str,
    transformed_values: dict[str, Any],
    warnings: list[Diagnostic],
    *,
    value_name: str,
) -> Any:
    del spec
    hash_parts: list[str] = []
    for field_id_value in field_ids:
        field_id = case_key(field_id_value)
        if field_id not in transformed_values:
            warnings.append(
                Diagnostic(
                    f"{value_name} omitted from unit-test expectation because a configured field is not implemented locally",
                    field_id_value,
                )
            )
            return _SKIP_EXPECTED
        value = transformed_values[field_id]
        hash_parts.append("" if value is None else str(value))
    return separator.join(hash_parts)


def _apply_unit_test_scd2_validity(
    spec: dict[str, Any],
    expected_rows: list[dict[str, Any]],
    transformed_rows: list[dict[str, Any]],
    warnings: list[Diagnostic],
) -> None:
    for expected_row, transformed_values in zip(expected_rows, transformed_rows, strict=True):
        deleted_flag = _unit_test_is_deleted_flag(spec, transformed_values, warnings)
        if deleted_flag is not _SKIP_EXPECTED:
            expected_row["IS_DELETED_FLAG"] = deleted_flag

    valid_from_values = [
        _unit_test_valid_from_datetime(spec, transformed_values, warnings)
        for transformed_values in transformed_rows
    ]
    if any(value is _SKIP_EXPECTED for value in valid_from_values):
        return
    parsed_valid_from_values = [value for value in valid_from_values if isinstance(value, datetime)]

    valid_to_values, output_valid_from_values = _unit_test_valid_to_datetimes(
        spec,
        transformed_rows,
        parsed_valid_from_values,
        warnings,
    )
    if valid_to_values is _SKIP_EXPECTED:
        return
    for expected_row, valid_from_value in zip(expected_rows, output_valid_from_values, strict=True):
        expected_row[_scd2_valid_from_column(spec)] = _format_unit_test_validity(spec, valid_from_value)
    for expected_row, valid_to_value in zip(expected_rows, valid_to_values, strict=True):
        formatted_valid_to = _format_unit_test_validity(spec, valid_to_value)
        expected_row[_scd2_valid_to_column(spec)] = formatted_valid_to
        expected_row["IS_CURRENT_FLAG"] = "Y" if formatted_valid_to == _scd2_end_of_time_unit_test_value(spec) else "N"


def _format_unit_test_validity(spec: dict[str, Any], value: datetime) -> str:
    return value.date().isoformat() if scd2_validity_data_type(spec) == "date" else _format_unit_test_datetime(value)


def _scd2_end_of_time_unit_test_value(spec: dict[str, Any]) -> str:
    return "9999-12-31" if scd2_validity_data_type(spec) == "date" else SCD2_END_OF_TIME


def _unit_test_valid_from_datetime(
    spec: dict[str, Any],
    transformed_values: dict[str, Any],
    warnings: list[Diagnostic],
) -> datetime | object:
    del transformed_values
    if _change_type(spec) == "scd2_derived":
        warnings.append(
            Diagnostic(
                "VALID_FROM_DATETIME omitted from unit-test expectation because scd2_derived expressions are SQL-evaluated",
                "$.control_data.scd.valid_from_datetime.expression",
            )
        )
        return _SKIP_EXPECTED
    value = _insert_time_value(spec)
    if value is None:
        warnings.append(Diagnostic("VALID_FROM_DATETIME omitted from unit-test expectation because insert_time is missing", "$.control_data.scd.insert_time"))
        return _SKIP_EXPECTED
    if isinstance(value, str) and "{{" in value:
        warnings.append(Diagnostic("VALID_FROM_DATETIME omitted from unit-test expectation because insert_time is runtime-defined", "$.control_data.scd.insert_time"))
        return _SKIP_EXPECTED
    parsed = _parse_unit_test_datetime(value)
    return parsed


def _unit_test_is_deleted_flag(
    spec: dict[str, Any],
    transformed_values: dict[str, Any],
    warnings: list[Diagnostic],
) -> str | object:
    if _delete_detection_mode(spec) != "field":
        return "N"
    config = _delete_detection_config(spec)
    field_id = case_key(str(config["field"]))
    if field_id not in transformed_values:
        warnings.append(Diagnostic("IS_DELETED_FLAG omitted from unit-test expectation because delete field is not implemented locally", str(config["field"])))
        return _SKIP_EXPECTED
    return "Y" if transformed_values[field_id] == config["value"] else "N"


def _unit_test_valid_to_datetimes(
    spec: dict[str, Any],
    transformed_rows: list[dict[str, Any]],
    valid_from_values: list[datetime],
    warnings: list[Diagnostic],
) -> tuple[list[datetime] | object, list[datetime]]:
    del warnings
    adjusted_valid_from = _continuous_unit_test_valid_from_datetimes(spec, transformed_rows, valid_from_values)
    return _continuous_unit_test_valid_to_datetimes(spec, transformed_rows, adjusted_valid_from), adjusted_valid_from


def _continuous_unit_test_valid_from_datetimes(
    spec: dict[str, Any],
    transformed_rows: list[dict[str, Any]],
    valid_from_values: list[datetime],
) -> list[datetime]:
    adjusted = list(valid_from_values)
    if not _scd2_auto_from_sot(spec):
        return adjusted
    for group in _unit_test_business_key_groups(spec, transformed_rows, adjusted):
        first_index = group[0]
        adjusted[first_index] = _parse_unit_test_datetime(_scd2_start_of_time(spec))
    return adjusted


def _continuous_unit_test_valid_to_datetimes(
    spec: dict[str, Any],
    transformed_rows: list[dict[str, Any]],
    valid_from_values: list[datetime],
) -> list[datetime]:
    end_of_time = "9999-12-31T00:00:00Z" if scd2_validity_data_type(spec) == "date" else SCD2_END_OF_TIME
    valid_to_values = [_parse_unit_test_datetime(end_of_time) for _ in valid_from_values]
    for group in _unit_test_business_key_groups(spec, transformed_rows, valid_from_values):
        for current_index, next_index in zip(group, group[1:]):
            next_value = valid_from_values[next_index]
            valid_to_values[current_index] = next_value - timedelta(days=1 if scd2_validity_data_type(spec) == "date" else 0)
    return valid_to_values


def _unit_test_business_key_groups(
    spec: dict[str, Any],
    transformed_rows: list[dict[str, Any]],
    valid_from_values: list[datetime],
) -> list[list[int]]:
    groups: dict[tuple[Any, ...], list[int]] = {}
    for index, transformed_values in enumerate(transformed_rows):
        key_value = _unit_test_business_key(spec, transformed_values, warnings=[])
        key = (index,) if key_value is _SKIP_EXPECTED else (key_value,)
        groups.setdefault(key, []).append(index)
    return [
        sorted(indexes, key=lambda index: valid_from_values[index])
        for indexes in groups.values()
    ]


def _parse_unit_test_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("unit-test timestamp value must include a timezone")
        return value.astimezone(timezone.utc)
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError("unit-test timestamp value must include a timezone")
    return parsed.astimezone(timezone.utc)


def _format_unit_test_datetime(value: datetime | object) -> str:
    if not isinstance(value, datetime):
        return str(value)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _field_expression(field: dict[str, Any]) -> str:
    expression, _ = _field_expression_and_parse_failures(field)
    return expression


def _field_expression_and_parse_failures(field: dict[str, Any]) -> tuple[str, list[str]]:
    expression = _quote_identifier(_source_column_name(field))
    failures: list[str] = []
    for transform in field.get("transforms", []):
        transform_type = transform.get("type")
        if transform_type == "trim":
            side = transform.get("side", "both")
            if side == "left":
                expression = f"ltrim({expression})"
            elif side == "right":
                expression = f"rtrim({expression})"
            else:
                expression = f"trim({expression})"
        elif transform_type == "round":
            expression = f"round({expression}, {int(transform['scale'])})"
        elif transform_type == "custom":
            macro_name = _macro_object_name(transform["macro"])
            expression = "{{ " + f"{macro_name}('{expression}')" + " }}"
        elif transform_type == "parse_date":
            parsed_expression = _parse_date_expression(expression, transform)
            failures.append(_parse_transform_failure_expression(field["id"], transform, expression, parsed_expression))
            expression = parsed_expression
        elif transform_type == "parse_timestamp":
            parsed_expression = _parse_timestamp_expression(expression, transform)
            failures.append(_parse_transform_failure_expression(field["id"], transform, expression, parsed_expression))
            expression = parsed_expression
    return expression, failures


def _parse_transform_failure_expression(
    field_id: str,
    transform: dict[str, Any],
    input_expression: str,
    parsed_expression: str,
) -> str:
    transform_type = transform["type"]
    transform_format = transform["format"]
    return (
        "case "
        f"when {input_expression} is not null and {parsed_expression} is null "
        f"then {_sql_string(f'field `{field_id}` does not match {transform_type} format `{transform_format}`')} "
        "end"
    )


def _parse_date_expression(expression: str, transform: dict[str, Any]) -> str:
    snowflake_format = _snowflake_datetime_format(str(transform["format"]))
    return f"try_to_date(cast({expression} as varchar), {_sql_string(snowflake_format)})"


def _parse_timestamp_expression(expression: str, transform: dict[str, Any]) -> str:
    python_format = str(transform["format"])
    snowflake_format = _snowflake_datetime_format(python_format)
    timezone_if_missing = transform.get("timezone_if_missing")
    if timezone_if_missing in {"Z", "UTC"}:
        parsed_expression = (
            f"try_to_timestamp_tz(concat(cast({expression} as varchar), ' +0000'), "
            f"{_sql_string(snowflake_format + ' TZHTZM')})"
        )
        return _time_if_missing_expression(parsed_expression, transform, python_format)
    if timezone_if_missing == "local":
        parsed_expression = f"to_timestamp_tz(try_to_timestamp_ntz(cast({expression} as varchar), {_sql_string(snowflake_format)}))"
        return _time_if_missing_expression(parsed_expression, transform, python_format)
    if not _python_datetime_format_has_timezone(python_format):
        raise ValueError(
            "`parse_timestamp` dbt generation requires a timezone directive in `format` "
            "or `timezone_if_missing`"
        )
    parsed_expression = f"try_to_timestamp_tz(cast({expression} as varchar), {_sql_string(snowflake_format)})"
    return _time_if_missing_expression(parsed_expression, transform, python_format)


def _time_if_missing_expression(expression: str, transform: dict[str, Any], python_format: str) -> str:
    if _python_datetime_format_has_time(python_format):
        return expression
    mode = transform.get("time_if_missing")
    if mode == "start_of_day":
        return f"date_trunc('day', {expression})"
    if mode == "end_of_day":
        return f"dateadd(nanosecond, -1, dateadd(day, 1, date_trunc('day', {expression})))"
    return expression


def _python_datetime_format_has_timezone(python_format: str) -> bool:
    index = 0
    while index < len(python_format):
        if python_format[index] != "%":
            index += 1
            continue
        if index + 1 >= len(python_format):
            raise ValueError("datetime format contains a trailing `%`")
        directive = python_format[index + 1]
        if directive in {"z", "Z"}:
            return True
        index += 2
    return False


def _python_datetime_format_has_time(python_format: str) -> bool:
    index = 0
    while index < len(python_format):
        if python_format[index] != "%":
            index += 1
            continue
        if index + 1 >= len(python_format):
            raise ValueError("datetime format contains a trailing `%`")
        directive = python_format[index + 1]
        if directive in {"H", "I", "M", "S", "f", "p"}:
            return True
        index += 2
    return False


def _snowflake_datetime_format(python_format: str) -> str:
    tokens = {
        "Y": "YYYY",
        "y": "YY",
        "m": "MM",
        "d": "DD",
        "H": "HH24",
        "I": "HH12",
        "M": "MI",
        "S": "SS",
        "f": "FF6",
        "z": "TZHTZM",
        "b": "MON",
        "B": "MONTH",
        "a": "DY",
        "A": "DAY",
        "j": "DDD",
        "p": "AM",
        "%": "%",
    }
    parts: list[str] = []
    index = 0
    while index < len(python_format):
        char = python_format[index]
        if char == "%":
            if index + 1 >= len(python_format):
                raise ValueError("datetime format contains a trailing `%`")
            directive = python_format[index + 1]
            if directive not in tokens:
                raise ValueError(f"datetime format directive `%{directive}` is not supported by dbt generation")
            parts.append(tokens[directive])
            index += 2
            continue
        if char.isalpha():
            parts.append(f'"{char}"')
        else:
            parts.append(char)
        index += 1
    return "".join(parts)


def _failure_details_expression(spec: dict[str, Any]) -> str:
    expressions = []
    for field in fields(spec):
        expressions.extend(_field_failure_expressions(field))
        expressions.extend(_field_uniqueness_failure_expressions(field))
    if not expressions:
        return "null"
    return f"coalesce({', '.join(expressions)})"


def _field_failure_expressions(field: dict[str, Any]) -> list[str]:
    field_id = field["id"]
    expression, parse_failures = _field_expression_and_parse_failures(field)
    data_type = field["data_type"]
    failures: list[str] = [*parse_failures]
    if field.get("nullable") is False:
        failures.append(
            f"case when {expression} is null then {_sql_string(f'field `{field_id}` is null but not nullable')} end"
        )

    sql_type = parse_sql_type(data_type)
    if sql_type.name in {"char", "varchar", "character", "string"} and sql_type.args:
        max_length = sql_type.args[0]
        failures.append(
            "case "
            f"when {expression} is not null and length({expression}) > {max_length} "
            f"then {_sql_string(f'field `{field_id}` exceeds data_type `{data_type}`')} "
            "end"
        )
    elif sql_type.name not in {"text"}:
        typed_expression = _try_cast_expression(expression, data_type)
        failures.append(
            "case "
            f"when {expression} is not null and {typed_expression} is null "
            f"then {_sql_string(f'field `{field_id}` does not match data_type `{data_type}`')} "
            "end"
        )

    for rule in field.get("validations", []):
        if not isinstance(rule, dict):
            continue
        rule_type = rule.get("type")
        if rule_type == "min_length":
            rule_value = rule["value"]
            failures.append(
                "case "
                f"when {expression} is not null and length({expression}) < {int(rule_value)} "
                f"then {_sql_string(f'field `{field_id}` length is less than {rule_value}')} "
                "end"
            )
        elif rule_type == "max_length":
            rule_value = rule["value"]
            failures.append(
                "case "
                f"when {expression} is not null and length({expression}) > {int(rule_value)} "
                f"then {_sql_string(f'field `{field_id}` length is greater than {rule_value}')} "
                "end"
            )
        elif rule_type == "regex":
            failures.append(
                "case "
                f"when {expression} is not null and not regexp_like({expression}, {_sql_string(str(rule['pattern']))}) "
                f"then {_sql_string(f'field `{field_id}` does not match regex')} "
                "end"
            )
        elif rule_type == "allowed_values":
            values = ", ".join(_sql_string(str(value)) for value in rule.get("values", []))
            failures.append(
                "case "
                f"when {expression} is not null and {expression} not in ({values}) "
                f"then {_sql_string(f'field `{field_id}` is not one of the allowed values')} "
                "end"
            )
        elif rule_type == "min_value":
            numeric_expression = _try_cast_expression(expression, "number")
            rule_value = rule["value"]
            failures.append(
                "case "
                f"when {expression} is not null and {numeric_expression} < {rule_value} "
                f"then {_sql_string(f'field `{field_id}` is less than {rule_value}')} "
                "end"
            )
        elif rule_type == "max_value":
            numeric_expression = _try_cast_expression(expression, "number")
            rule_value = rule["value"]
            failures.append(
                "case "
                f"when {expression} is not null and {numeric_expression} > {rule_value} "
                f"then {_sql_string(f'field `{field_id}` is greater than {rule_value}')} "
                "end"
            )
        elif rule_type == "precision":
            precision = int(rule["precision"])
            scale = int(rule["scale"]) if "scale" in rule else 0
            typed_expression = _try_cast_expression(expression, f"number({precision}, {scale})")
            failures.append(
                "case "
                f"when {expression} is not null and {typed_expression} is null "
                f"then {_sql_string(f'field `{field_id}` does not fit validation precision/scale')} "
                "end"
            )
        elif rule_type == "custom":
            macro_name = _macro_object_name(rule["macro"])
            failures.append("{{ " + f"{macro_name}('{expression}')" + " }}")
    return failures


def _field_uniqueness_failure_expressions(field: dict[str, Any]) -> list[str]:
    if field.get("unique") is not True:
        return []
    field_id = field["id"]
    expression = _field_expression(field)
    typed_expression = _try_cast_expression(expression, field["data_type"])
    return [
        "case "
        f"when {typed_expression} is not null "
        f"and count(*) over (partition by {typed_expression}) > 1 "
        f"then {_sql_string(f'field `{field_id}` duplicates a value for a unique field')} "
        "end"
    ]


def _try_cast_expression(expression: str, data_type: str) -> str:
    return f"try_cast(cast({expression} as varchar) as {data_type})"


def _validation_rows_cte(spec: dict[str, Any], *, trailing_comma: bool) -> list[str]:
    if _change_type(spec) == "scd2_manual":
        return _scd2_manual_validation_rows_cte(spec, trailing_comma=trailing_comma)
    suffix = "," if trailing_comma else ""
    return [
        "validation_rows as (",
        "    select",
        "        *,",
        f"        {_failure_details_expression(spec)} as FAILURE_DETAILS",
        "    from source_rows",
        f"){suffix}",
    ]


def _scd2_manual_validation_rows_cte(spec: dict[str, Any], *, trailing_comma: bool) -> list[str]:
    suffix = "," if trailing_comma else ""
    target_relation = _target_relation_config(spec)
    business_key_columns = _business_key_partition_columns(spec)
    incremental_key_columns = _scd2_manual_incremental_unique_key_columns(spec)
    business_key_join = _candidate_join_condition("incoming_manual_rows", "existing_target", business_key_columns)
    replaced_row_join = _candidate_join_condition("incoming_manual_rows", "existing_manual_rows", incremental_key_columns)
    invalid_key_join = _candidate_join_condition("incoming_manual_rows", "manual_invalid_candidate_rows", business_key_columns)
    partition_by = ", ".join(business_key_columns)
    valid_from_column = _scd2_valid_from_column(spec)
    valid_to_column = _scd2_valid_to_column(spec)
    is_current_column = _physical_name("is_current_flag")
    return [
        "{% if flags.FULL_REFRESH %}",
        "{% set tms_manual_scd2_target_relation = none %}",
        "{% else %}",
        _dbt_relation_lookup(target_relation, "tms_manual_scd2_target_relation", schema_var="target_schema"),
        "{% endif %}",
        "base_validation_rows as (",
        "    select",
        "        *,",
        "        row_number() over (order by " + ", ".join(_source_output_columns(spec)) + ") as TMS_SOURCE_ROW_NUMBER,",
        f"        {_failure_details_expression(spec)} as TMS_FIELD_FAILURE_DETAILS",
        "    from source_rows",
        "),",
        "source_valid_rows as (",
        "    select *",
        "    from base_validation_rows",
        "    where TMS_FIELD_FAILURE_DETAILS is null",
        "),",
        "incoming_manual_rows as (",
        "    select",
        ",\n".join(_scd2_manual_incoming_candidate_select_lines(spec)),
        "    from source_valid_rows",
        "),",
        "existing_manual_rows as (",
        "{% if tms_manual_scd2_target_relation is not none %}",
        "    select",
        ",\n".join(_scd2_manual_existing_candidate_select_lines(spec)),
        "    from {{ tms_manual_scd2_target_relation }} as existing_target",
        "    where exists (",
        "        select 1",
        "        from incoming_manual_rows",
        f"        where {business_key_join}",
        "    )",
        "{% else %}",
        "    select",
        ",\n".join(_scd2_manual_empty_candidate_select_lines(spec)),
        "    where 1 = 0",
        "{% endif %}",
        "),",
        "remaining_existing_manual_rows as (",
        "    select *",
        "    from existing_manual_rows",
        *_scd2_manual_replaced_existing_filter_lines(spec, replaced_row_join),
        "),",
        "manual_candidate_rows as (",
        "    select * from remaining_existing_manual_rows",
        "    union all",
        "    select * from incoming_manual_rows",
        "),",
        "manual_windowed_candidate_rows as (",
        "    select",
        "        *,",
        f"        count_if({is_current_column} = 'Y') over (partition by {partition_by}) as TMS_CURRENT_ROW_COUNT,",
        f"        lead({valid_from_column}) over (partition by {partition_by} order by {valid_from_column}) as TMS_NEXT_VALID_FROM_DATETIME",
        "    from manual_candidate_rows",
        "),",
        "manual_invalid_candidate_rows as (",
        "    select",
        "        *,",
        "        coalesce(",
        (
            f"            case when TMS_CURRENT_ROW_COUNT > 1 "
            f"then {_sql_string('scd2_manual has multiple current rows for the same current-row key')} end,"
        ),
        (
            f"            case when {valid_to_column} is not null and {valid_from_column} is not null "
            f"and {valid_to_column} <= {valid_from_column} "
            f"then {_sql_string('scd2_manual valid_to must be after valid_from')} end,"
        ),
        (
            f"            case when TMS_NEXT_VALID_FROM_DATETIME is not null and {valid_to_column} is not null "
            f"and TMS_NEXT_VALID_FROM_DATETIME <= {valid_to_column} "
            f"then {_sql_string('scd2_manual validity windows overlap for the same business key')} end"
        ),
        "        ) as TMS_MANUAL_SCD2_FAILURE_DETAILS",
        "    from manual_windowed_candidate_rows",
        "    where TMS_CURRENT_ROW_COUNT > 1",
        f"       or ({valid_to_column} is not null and {valid_from_column} is not null and {valid_to_column} <= {valid_from_column})",
        f"       or (TMS_NEXT_VALID_FROM_DATETIME is not null and {valid_to_column} is not null and TMS_NEXT_VALID_FROM_DATETIME <= {valid_to_column})",
        "),",
        "manual_state_failures as (",
        "    select",
        "        incoming_manual_rows.TMS_SOURCE_ROW_NUMBER,",
        "        max(manual_invalid_candidate_rows.TMS_MANUAL_SCD2_FAILURE_DETAILS) as FAILURE_DETAILS",
        "    from incoming_manual_rows",
        "    join manual_invalid_candidate_rows",
        f"      on {invalid_key_join}",
        "    group by incoming_manual_rows.TMS_SOURCE_ROW_NUMBER",
        "),",
        "validation_rows as (",
        "    select",
        "        base_validation_rows.*,",
        "        coalesce(base_validation_rows.TMS_FIELD_FAILURE_DETAILS, manual_state_failures.FAILURE_DETAILS) as FAILURE_DETAILS",
        "    from base_validation_rows",
        "    left join manual_state_failures",
        "      on manual_state_failures.TMS_SOURCE_ROW_NUMBER = base_validation_rows.TMS_SOURCE_ROW_NUMBER",
        f"){suffix}",
    ]


def _scd2_manual_replaced_existing_filter_lines(spec: dict[str, Any], replaced_row_join: str) -> list[str]:
    if not _scd2_manual_uses_business_key_upsert(spec):
        return []
    return [
        "    where not exists (",
        "        select 1",
        "        from incoming_manual_rows",
        f"        where {replaced_row_join}",
        "    )",
    ]


def _scd2_manual_incoming_candidate_select_lines(spec: dict[str, Any]) -> list[str]:
    lines = [
        "        TMS_SOURCE_ROW_NUMBER",
        "        'Y' as TMS_IS_INCOMING_ROW",
    ]
    for field in _input_fields(spec):
        lines.append(
            f"        cast({_field_expression(field)} as {field['data_type']}) as {_quote_identifier(field['id'])}"
        )
    lines.extend(f"        {line.strip()}" for line in _business_key_select_lines(spec))
    return lines


def _scd2_manual_existing_candidate_select_lines(spec: dict[str, Any]) -> list[str]:
    lines = [
        "        cast(null as number) as TMS_SOURCE_ROW_NUMBER",
        "        'N' as TMS_IS_INCOMING_ROW",
    ]
    for field in _input_fields(spec):
        column = _quote_identifier(field["id"])
        lines.append(f"        existing_target.{column} as {column}")
    if _business_key_enabled(spec):
        business_key_column = _business_key_column(spec)
        lines.append(f"        existing_target.{business_key_column} as {business_key_column}")
    return lines


def _scd2_manual_empty_candidate_select_lines(spec: dict[str, Any]) -> list[str]:
    lines = [
        "        cast(null as number) as TMS_SOURCE_ROW_NUMBER",
        "        cast(null as varchar(1)) as TMS_IS_INCOMING_ROW",
    ]
    for field in _input_fields(spec):
        lines.append(f"        cast(null as {field['data_type']}) as {_quote_identifier(field['id'])}")
    if _business_key_enabled(spec):
        lines.append(f"        cast(null as {BUSINESS_KEY_DATA_TYPE}) as {_business_key_column(spec)}")
    return lines


def _candidate_join_condition(left_alias: str, right_alias: str, columns: list[str]) -> str:
    if not columns:
        return "1 = 1"
    return " and ".join(
        f"(({left_alias}.{column} = {right_alias}.{column}) or "
        f"({left_alias}.{column} is null and {right_alias}.{column} is null))"
        for column in columns
    )


def _valid_rows_cte() -> list[str]:
    return [
        "valid_rows as (",
        "    select *",
        "    from validation_rows",
        "    where FAILURE_DETAILS is null",
        ")",
    ]


def _custom_macro_names(spec: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for field in fields(spec):
        source = field.get("source")
        if isinstance(source, dict) and isinstance(source.get("macro"), str):
            names.append(source["macro"])
        for rule_group_name in ("transforms", "validations"):
            for rule in field.get(rule_group_name, []):
                if isinstance(rule, dict) and rule.get("type") == "custom":
                    names.append(rule["macro"])
    return sorted(set(names))


def _change_type(spec: dict[str, Any]) -> str | None:
    control_data = spec.get("control_data", {})
    if not isinstance(control_data, dict):
        return None
    value = control_data.get("change_type")
    return str(value) if value is not None else None


def _scd_config(spec: dict[str, Any]) -> dict[str, Any]:
    control_data = spec.get("control_data", {})
    if not isinstance(control_data, dict):
        return {}
    scd = control_data.get("scd", {})
    return scd if isinstance(scd, dict) else {}


def _valid_from_datetime_config(spec: dict[str, Any]) -> dict[str, Any]:
    configured = scd2_validity_config(spec).get("valid_from")
    if isinstance(configured, dict) and isinstance(configured.get("source"), dict):
        return configured
    if _change_type(spec) == "scd2_derived":
        return {}
    value = _insert_time_value(spec)
    return {"value": value} if value is not None else {}


def _insert_time_value(spec: dict[str, Any]) -> Any:
    return _scd_config(spec).get("insert_time")


def _scd2_auto_from_sot(spec: dict[str, Any]) -> bool:
    if _change_type(spec) == "scd2_derived":
        return False
    return _scd_config(spec).get("scd2_auto_from_sot", True) is not False


def _scd2_validation_mode(spec: dict[str, Any]) -> str:
    value = _scd_config(spec).get("scd2_validation", "continuous")
    return str(value) if value in {"continuous", "sparse"} else "continuous"


def _scd2_validation_enabled(spec: dict[str, Any]) -> bool:
    return _scd_config(spec).get("scd2_validation_enabled", True) is not False


def _delete_detection_config(spec: dict[str, Any]) -> dict[str, Any]:
    config = _scd_config(spec).get("delete_detection", {})
    return config if isinstance(config, dict) else {"mode": "never"}


def _delete_detection_mode(spec: dict[str, Any]) -> str:
    return str(_delete_detection_config(spec).get("mode", "never"))


def _truncate_before_load_enabled(spec: dict[str, Any]) -> bool:
    control_data = spec.get("control_data", {})
    return isinstance(control_data, dict) and control_data.get("truncate_before_load") is True


def _scd1_delete_filter_lines(spec: dict[str, Any]) -> list[str]:
    if _change_type(spec) != "scd1" or _delete_detection_mode(spec) != "field":
        return []
    return [f"where not ({_delete_detection_field_condition(spec)})"]


def _is_deleted_flag_expression(spec: dict[str, Any]) -> str:
    if _change_type(spec) == "scd2_derived":
        config = _scd_config(spec).get("deleted_flag", {})
        if isinstance(config, dict) and config.get("mode", "fixed") == "fixed":
            return _sql_scalar(config.get("value", "N"))
        return "'N'"
    mode = _delete_detection_mode(spec)
    if mode == "field":
        return f"case when {_delete_detection_field_condition(spec)} then 'Y' else 'N' end"
    return "'N'"


def _delete_detection_field_condition(spec: dict[str, Any]) -> str:
    config = _delete_detection_config(spec)
    field = _field_by_id(spec, str(config["field"]))
    return f"{_field_expression(field)} = {_sql_scalar(config['value'])}"


def _valid_from_datetime_expression(spec: dict[str, Any]) -> str:
    config = _valid_from_datetime_config(spec)
    if _change_type(spec) in {"scd2_derived", "scd2_manual"}:
        return f"cast({_field_expression(_field_by_id(spec, _scd2_valid_from_column(spec)))} as {_scd2_validity_data_type(spec)})"
    return f"cast({_sql_scalar(config['value'])} as {_scd2_timestamp_data_type(spec)})"


def _valid_to_datetime_expression(spec: dict[str, Any]) -> str:
    # Validity windows are inclusive, so an auto-SCD2 row ends one timestamp tick
    # before the next version starts.
    offset = _valid_to_datetime_offset(spec)
    return (
        "coalesce("
        f"dateadd({offset['unit']}, {offset['value']}, "
        f"lead({_scd2_valid_from_column(spec)}) over ({_scd2_window_clause(spec, _scd2_valid_from_column(spec))}))"
        ", "
        f"{_scd2_end_of_time_expression(spec)}"
        ")"
    )


def _valid_to_datetime_offset(spec: dict[str, Any]) -> dict[str, Any]:
    return _scd2_validity_offset(spec)


def _scd2_validity_offset(spec: dict[str, Any]) -> dict[str, Any]:
    validity = scd2_validity_config(spec)
    config = validity.get("valid_to") if isinstance(validity.get("valid_to"), dict) else _scd_config(spec).get("valid_to_datetime", {})
    if isinstance(config, dict):
        offset = config.get("offset", {})
        if isinstance(offset, dict):
            unit = str(offset.get("unit", "day" if _scd2_validity_data_type(spec) == "date" else "nanosecond"))
            value = offset.get("value", -1)
            allowed_units = {"day"} if _scd2_validity_data_type(spec) == "date" else {"nanosecond", "second"}
            if unit in allowed_units and isinstance(value, int):
                return {"unit": unit, "value": value}
    return {"unit": "day" if _scd2_validity_data_type(spec) == "date" else "nanosecond", "value": -1}


def _scd2_end_of_time_expression(spec: dict[str, Any]) -> str:
    validity = scd2_validity_config(spec)
    config = validity.get("valid_to") if isinstance(validity.get("valid_to"), dict) else _scd_config(spec).get("valid_to_datetime", {})
    if isinstance(config, dict) and config.get("end_of_time") is not None:
        return f"cast({_sql_string(str(config['end_of_time']))} as {_scd2_validity_data_type(spec)})"
    return f"cast({_sql_string('9999-12-31' if _scd2_validity_data_type(spec) == 'date' else SCD2_END_OF_TIME)} as {_scd2_validity_data_type(spec)})"


def _scd2_timestamp_data_type(spec: dict[str, Any]) -> str:
    return _scd2_validity_data_type(spec)


def _scd2_validity_data_type(spec: dict[str, Any]) -> str:
    """Compatibility alias for validity SQL helpers."""
    return scd2_validity_data_type(spec)


def _scd2_start_of_time(spec: dict[str, Any]) -> str:
    return "0001-01-01" if _scd2_validity_data_type(spec) == "date" else SCD2_START_OF_TIME


def _scd2_end_of_time_validation_threshold(spec: dict[str, Any]) -> str:
    return "9999-12-30" if _scd2_validity_data_type(spec) == "date" else SCD2_END_OF_TIME_VALIDATION_THRESHOLD


def _scd_validation_scope(spec: dict[str, Any]) -> str:
    value = _scd_config(spec).get("validation_scope", "affected_window")
    return str(value) if value in {"affected_window", "full_target"} else "affected_window"


def _scd2_window_clause(spec: dict[str, Any], order_column: str) -> str:
    return f"partition by {', '.join(_business_key_partition_columns(spec))} order by {order_column}"


def _business_key_config(spec: dict[str, Any]) -> dict[str, Any]:
    control_data = spec.get("control_data", {})
    if not isinstance(control_data, dict):
        return {}
    config = control_data.get("business_key", {})
    return config if isinstance(config, dict) else {}


def _business_key_field_ids(spec: dict[str, Any]) -> list[str]:
    values = _business_key_config(spec).get("fields", [])
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if isinstance(value, str)]


def _business_key_fields(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [_field_by_id(spec, field_id) for field_id in _business_key_field_ids(spec)]


def _business_key_column(spec: dict[str, Any]) -> str:
    target = spec.get("target", {})
    target_id = target.get("id") if isinstance(target, dict) else None
    return _physical_name(f"{_target_key_base(target_id)}_business_key" if isinstance(target_id, str) else "business_key")


def _business_key_expression(spec: dict[str, Any]) -> str:
    value_expression = _field_concat_expression(_business_key_fields(spec), "|")
    return f"cast(sha2({value_expression}, 256) as {BUSINESS_KEY_DATA_TYPE})"


def _business_key_enabled(spec: dict[str, Any]) -> bool:
    control_data = spec.get("control_data", {})
    if not isinstance(control_data, dict):
        return True
    return control_data.get("skip_business_key") is not True


def _business_key_required_for_generation(spec: dict[str, Any]) -> bool:
    return _business_key_enabled(spec) or _generated_scd2_enabled(spec)


def _business_key_select_lines(spec: dict[str, Any]) -> list[str]:
    if not _business_key_enabled(spec):
        return []
    return [f"    {_business_key_expression(spec)} as {_business_key_column(spec)}"]


def _business_key_output_lines(spec: dict[str, Any]) -> list[str]:
    if not _business_key_enabled(spec):
        return []
    return [f"    {_business_key_column(spec)}"]


def _business_key_change_row_columns(spec: dict[str, Any]) -> list[str]:
    if not _business_key_enabled(spec):
        return []
    return [_business_key_column(spec)]


def _business_key_existing_target_select_lines(spec: dict[str, Any]) -> list[str]:
    if not _business_key_enabled(spec):
        return []
    return [f"        existing_target.{_business_key_column(spec)} as {_business_key_column(spec)}"]


def _business_key_null_select_lines(spec: dict[str, Any]) -> list[str]:
    if not _business_key_enabled(spec):
        return []
    return [f"        cast(null as {BUSINESS_KEY_DATA_TYPE}) as {_business_key_column(spec)}"]


def _business_key_partition_columns(spec: dict[str, Any]) -> list[str]:
    if _business_key_enabled(spec):
        return [_business_key_column(spec)]
    return [_quote_identifier(field["id"]) for field in _business_key_fields(spec)]


def _business_key_unique_key_columns(spec: dict[str, Any]) -> list[str]:
    if _business_key_enabled(spec):
        return [_business_key_column(spec)]
    return [_physical_name(field["id"]) for field in _business_key_fields(spec)]


def _business_key_cte_select_lines(spec: dict[str, Any], indent: str) -> list[str]:
    return [f"{indent}{column}" for column in _business_key_partition_columns(spec)]


def _surrogate_key_enabled(spec: dict[str, Any]) -> bool:
    control_data = spec.get("control_data", {})
    if not isinstance(control_data, dict):
        return True
    return control_data.get("skip_surrogate_key") is not True


def _surrogate_key_column(spec: dict[str, Any]) -> str:
    target = spec.get("target", {})
    target_id = target.get("id") if isinstance(target, dict) else None
    return _physical_name(f"{_target_key_base(target_id)}_key" if isinstance(target_id, str) else "key")


def _target_key_base(target_id: str) -> str:
    return target_id.rsplit("__", 1)[-1]


def _surrogate_key_select_lines(spec: dict[str, Any]) -> list[str]:
    if not _surrogate_key_enabled(spec):
        return []
    if _generated_scd2_enabled(spec):
        # Generated SCD2 models create the key only after the final validity
        # window has been calculated.  Keep this intermediate column solely so
        # the shared change-row CTE shape remains consistent.
        return [
            f"    cast(null as {SURROGATE_KEY_DATA_TYPE}) as {_surrogate_key_column(spec)}"
        ]
    return [
        f"    cast(uuid_string() as {SURROGATE_KEY_DATA_TYPE}) as {_surrogate_key_column(spec)}"
    ]


def _surrogate_key_output_lines(spec: dict[str, Any]) -> list[str]:
    if not _surrogate_key_enabled(spec):
        return []
    if _generated_scd2_enabled(spec):
        return [
            f"    cast(uuid_string('{SCD2_SURROGATE_KEY_NAMESPACE}', "
            f"concat_ws('|', coalesce(cast({_business_key_column(spec)} as varchar), ''), "
            f"to_char(cast({_scd2_valid_from_column(spec)} as date), 'YYYY-MM-DD'))) "
            f"as {SURROGATE_KEY_DATA_TYPE}) as {_surrogate_key_column(spec)}"
        ]
    return [f"    {_surrogate_key_column(spec)}"]


def _surrogate_key_change_row_columns(spec: dict[str, Any]) -> list[str]:
    if not _surrogate_key_enabled(spec):
        return []
    return [_surrogate_key_column(spec)]


def _surrogate_key_existing_target_select_lines(spec: dict[str, Any]) -> list[str]:
    if not _surrogate_key_enabled(spec):
        return []
    return [
        f"        existing_target.{_surrogate_key_column(spec)} as {_surrogate_key_column(spec)}"
    ]


def _surrogate_key_null_select_lines(spec: dict[str, Any]) -> list[str]:
    if not _surrogate_key_enabled(spec):
        return []
    return [f"        cast(null as {SURROGATE_KEY_DATA_TYPE}) as {_surrogate_key_column(spec)}"]


def _field_by_id(spec: dict[str, Any], field_id: str) -> dict[str, Any]:
    key = case_key(field_id)
    for field in _input_fields(spec):
        if isinstance(field, dict) and isinstance(field.get("id"), str) and case_key(field["id"]) == key:
            return field
    raise ValueError(f"field `{field_id}` does not exist")


def _business_data_hash_expression(spec: dict[str, Any]) -> str:
    hash_fields = _business_data_hash_fields(spec)
    value_expression = _field_concat_expression(hash_fields, "|")
    return (
        f"cast(sha2({value_expression}, 256) "
        f"as {GENERATED_METADATA_FIELD_TYPES['business_data_hash']})"
    )


def _business_data_hash_select_lines(spec: dict[str, Any]) -> list[str]:
    if not _business_data_hash_enabled(spec):
        return []
    return [f"    {_business_data_hash_expression(spec)} as BUSINESS_DATA_HASH"]


def _business_data_hash_enabled(spec: dict[str, Any]) -> bool:
    config = _business_data_hash_config(spec)
    return config.get("skip_business_data_hash") is not True


def _field_concat_expression(configured_fields: list[dict[str, Any]], separator: str) -> str:
    if not configured_fields:
        return "''"
    field_values = [
        "coalesce("
        f"cast(cast({_field_expression(field)} as {field['data_type']}) as varchar), "
        "''"
        ")"
        for field in configured_fields
    ]
    return "concat_ws(" + _sql_string(separator) + ", " + ", ".join(field_values) + ")"


def _business_data_hash_fields(spec: dict[str, Any]) -> list[dict[str, Any]]:
    target_fields = fields(spec)
    fields_by_id = {
        case_key(field["id"]): field
        for field in target_fields
        if isinstance(field, dict) and isinstance(field.get("id"), str)
    }
    config = _business_data_hash_config(spec)
    mode = config.get("business_data_hash_mode", "exclude")
    configured_fields = _business_data_hash_field_ids(spec)
    excluded_fields = _business_data_hash_ineligible_field_ids(spec)
    if mode == "include":
        return [
            fields_by_id[field_id]
            for field_id in configured_fields
            if field_id in fields_by_id and field_id not in excluded_fields
        ]
    excluded_fields.update(configured_fields)
    return [
        field
        for field in target_fields
        if isinstance(field, dict)
        and isinstance(field.get("id"), str)
        and case_key(field["id"]) not in excluded_fields
    ]


def _business_data_hash_config(spec: dict[str, Any]) -> dict[str, Any]:
    control_data = spec.get("control_data", {})
    if not isinstance(control_data, dict):
        return {}
    config = control_data.get("business_data_hash", {})
    return config if isinstance(config, dict) else {}


def _business_data_hash_field_ids(spec: dict[str, Any]) -> list[str]:
    configured_fields = _business_data_hash_config(spec).get("fields", [])
    if not isinstance(configured_fields, list):
        return []
    return [case_key(field_id) for field_id in configured_fields if isinstance(field_id, str)]


def _business_data_hash_ineligible_field_ids(spec: dict[str, Any]) -> set[str]:
    excluded_fields = set(RESERVED_GENERATED_FIELDS)
    excluded_fields.update(case_key(field_id) for field_id in SOURCE_AUDIT_COLUMN_IDS)
    if _surrogate_key_enabled(spec):
        excluded_fields.add(case_key(_surrogate_key_column(spec)))
    if _business_key_enabled(spec):
        excluded_fields.add(case_key(_business_key_column(spec)))
    if _change_type(spec) == "scd2_manual":
        excluded_fields.update(SCD2_MANUAL_FIELD_TYPES)
        excluded_fields.update(case_key(field_id) for field_id in _scd2_manual_update_key_field_ids(spec))
    return excluded_fields


def _quarantine_model_name(target_id: str) -> str:
    return f"{target_id}__quarantine"


def _validation_guard_model_name(target_id: str) -> str:
    return f"{target_id}__validation_guard"


def _validation_enabled(spec: dict[str, Any]) -> bool:
    control_data = spec.get("control_data", {})
    return not (isinstance(control_data, dict) and control_data.get("validation_enabled") is False)


def _fail_load_enabled(spec: dict[str, Any]) -> bool:
    return _validation_enabled(spec) and _failure_mode(spec) == "fail_load"


def _quarantine_enabled(spec: dict[str, Any]) -> bool:
    control_data = spec.get("control_data", {})
    return _validation_enabled(spec) and isinstance(control_data, dict) and control_data.get("failure_mode") == "quarantine_row"


def _failure_mode(spec: dict[str, Any]) -> str:
    control_data = spec.get("control_data", {})
    if not isinstance(control_data, dict):
        return "fail_load"
    return str(control_data.get("failure_mode", "fail_load"))


def _write(path: Path, content: str, result: DbtGenerationResult) -> None:
    path.write_text(content, encoding="utf-8")
    result.files.append(path)


def _dedupe_diagnostics(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    seen: set[tuple[str | None, str]] = set()
    deduped: list[Diagnostic] = []
    for diagnostic in diagnostics:
        key = (diagnostic.location, diagnostic.message)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(diagnostic)
    return deduped
