import csv
import hashlib
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .custom_macros import MacroLoadError, PythonMacroResolver
from .errors import Diagnostic
from .inheritance import InheritanceError, resolve_spec
from .schema import require_yaml
from .spec import GENERATED_METADATA_FIELD_TYPES, case_key, fields, parse_sql_type


DBT_PROJECT_NAME = "type_materialisation_generated"
DBT_PROFILE_NAME = "datahub_type_materialisation"
SCD2_START_OF_TIME = "0001-01-01T00:00:00Z"
SCD2_END_OF_TIME = "9999-12-31T23:59:59Z"

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


@dataclass(frozen=True)
class RelationConfig:
    database: str | None
    schema: str
    table: str


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
    diagnostics = _unsupported_for_initial_dbt_generation(spec)
    if diagnostics:
        result.errors.extend(diagnostics)
        return result

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
    if _failure_mode(spec) == "fail_load":
        _write_validation_guard_model(spec, result)
    _write_generated_macros(options.output_dir, generated_macros, result)
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
    if _change_type(spec) == "scd2" and scd.get("valid_from_to_mode", "continuous") == "sparse":
        diagnostics.append(
            Diagnostic("valid_from_to_mode `sparse` is not implemented yet", "$.control_data.scd.valid_from_to_mode")
        )
    if _change_type(spec) == "scd2":
        valid_from = scd.get("valid_from_datetime", {})
        if not isinstance(valid_from, dict):
            valid_from = {}
        delete_detection = scd.get("delete_detection", {})
        if not isinstance(delete_detection, dict):
            delete_detection = {}
        if (
            delete_detection.get("mode") == "missing_from_source"
            and valid_from.get("valid_from_datetime_selection") == "field"
        ):
            diagnostics.append(
                Diagnostic(
                    "delete_detection.mode `missing_from_source` is invalid when valid_from_datetime_selection is field",
                    "$.control_data.scd.delete_detection.mode",
                )
            )
    return diagnostics


def _ensure_dirs(output_dir: Path) -> None:
    (output_dir / "models" / "generated").mkdir(parents=True, exist_ok=True)
    (output_dir / "macros" / "generated").mkdir(parents=True, exist_ok=True)
    (output_dir / "macros" / "reference").mkdir(parents=True, exist_ok=True)
    (output_dir / "seeds").mkdir(parents=True, exist_ok=True)


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


def _write_status_file(output_dir: Path, result: DbtGenerationResult) -> None:
    lines = [
        "# Not Yet Implemented",
        "",
        "This generated dbt project covers the first implementation slice only.",
        "",
    ]
    lines.extend(f"- {item}" for item in NOT_IMPLEMENTED)
    _write(output_dir / "NOT_IMPLEMENTED.md", "\n".join(lines) + "\n", result)


def _write_source_model(spec: dict[str, Any], options: GenerateDbtOptions, result: DbtGenerationResult) -> None:
    source = spec["source"]
    if source["format"] == "table":
        _write_table_source_model(spec, options, result)
    else:
        _write_csv_source_model(spec, options, result)


def _write_csv_source_model(spec: dict[str, Any], options: GenerateDbtOptions, result: DbtGenerationResult) -> None:
    if _csv_load_method(spec["source"]) == "dbt_seed":
        _write_csv_seed_source_model(spec, options, result)
        return

    target = spec["target"]
    model_name = _source_model_name(target["id"])
    stage = _stage_reference(options.csv_stage) if options.csv_stage else _csv_stage_location(spec["source"])
    select_lines = []
    for field in fields(spec):
        source = field.get("source", {})
        pos = source.get("pos")
        column = _source_column_name(field)
        ordinal = int(pos) + 1
        select_lines.append(f"    ${ordinal}::string as {_quote_identifier(column)}")
    sql = "\n".join(
        [
            f"{{{{ config(materialized='view', alias='{_physical_name(model_name)}') }}}}",
            "",
            "select",
            ",\n".join(select_lines),
            f"from {stage}",
            "",
        ]
    )
    _write(options.output_dir / "models" / "generated" / f"{model_name}.sql", sql, result)


def _write_csv_seed_source_model(spec: dict[str, Any], options: GenerateDbtOptions, result: DbtGenerationResult) -> None:
    target = spec["target"]
    model_name = _source_model_name(target["id"])
    seed_name = _csv_seed_name(spec)
    try:
        seed_path = _write_csv_seed_file(spec, options, result)
    except OSError as exc:
        result.errors.append(Diagnostic(str(exc), "$.source.seed.file"))
        return

    select_lines = []
    for field in fields(spec):
        column = _source_column_name(field)
        select_lines.append(f"    cast({_quote_identifier(column)} as string) as {_quote_identifier(column)}")
    sql = "\n".join(
        [
            f"{{{{ config(materialized='view', alias='{_physical_name(model_name)}') }}}}",
            "",
            "select",
            ",\n".join(select_lines),
            f"from {{{{ ref('{seed_name}') }}}}",
            "",
        ]
    )
    _write(result.output_dir / "models" / "generated" / f"{model_name}.sql", sql, result)
    result.files.append(seed_path)


def _write_csv_seed_file(spec: dict[str, Any], options: GenerateDbtOptions, result: DbtGenerationResult) -> Path:
    seed_file = _csv_seed_file_path(spec, options.spec_path)
    seed_name = _csv_seed_name(spec)
    target_path = result.output_dir / "seeds" / f"{seed_name}.csv"
    if spec["source"].get("header") is False:
        _write_headerless_seed_file(spec, seed_file, target_path)
    else:
        shutil.copyfile(seed_file, target_path)
    return target_path


def _write_headerless_seed_file(spec: dict[str, Any], seed_file: Path, target_path: Path) -> None:
    source = spec["source"]
    dialect = {
        "delimiter": source.get("delimiter", ","),
        "quotechar": source.get("quotechar", '"'),
        "lineterminator": source.get("lineterminator", "\r\n"),
    }
    with seed_file.open("r", encoding="utf-8", newline="") as source_handle:
        reader = csv.reader(
            source_handle,
            delimiter=dialect["delimiter"],
            quotechar=dialect["quotechar"],
        )
        rows = list(reader)
        max_pos = max(
            int(field.get("source", {}).get("pos", 0))
            for field in fields(spec)
        )
        column_count = max([max_pos + 1, *(len(row) for row in rows)])
        header = [_position_column_name(index) for index in range(column_count)]
        with target_path.open("w", encoding="utf-8", newline="") as target_handle:
            writer = csv.writer(
                target_handle,
                delimiter=dialect["delimiter"],
                quotechar=dialect["quotechar"],
                lineterminator=dialect["lineterminator"],
            )
            writer.writerow(header)
            writer.writerows(rows)


def _write_table_source_model(spec: dict[str, Any], options: GenerateDbtOptions, result: DbtGenerationResult) -> None:
    del options
    source = spec["source"]
    target = spec["target"]
    model_name = _source_model_name(target["id"])
    select_lines = []
    for field in fields(spec):
        column = field.get("source", {}).get("column", field["id"])
        select_lines.append(f"    {_quote_identifier(column)} as {_quote_identifier(column)}")
    sql = "\n".join(
        [
            f"{{{{ config(materialized='view', alias='{_physical_name(model_name)}') }}}}",
            "",
            "select",
            ",\n".join(select_lines),
            f"from {_table_source_relation(source)}",
            "",
        ]
    )
    _write(result.output_dir / "models" / "generated" / f"{model_name}.sql", sql, result)


def _write_final_model(spec: dict[str, Any], result: DbtGenerationResult) -> None:
    target = spec["target"]
    table_name = target.get("table_name", target["id"])
    materialized = spec.get("control_data", {}).get("materialisation_type", "table")
    source_model = _source_model_name(target["id"])
    quarantine_enabled = _quarantine_enabled(spec)
    fail_load_enabled = _failure_mode(spec) == "fail_load"
    field_select_lines = []
    for field in fields(spec):
        expression = _field_expression(field)
        data_type = field["data_type"]
        field_select_lines.append(f"    cast({expression} as {data_type}) as {_quote_identifier(field['id'])}")
    audit_select_lines = _audit_select_lines()
    extra_config_lines: list[str] = []
    if _scd2_uses_target_merge(spec):
        materialized = "incremental"
        extra_config_lines = [
            "    incremental_strategy='delete+insert',",
            f"    unique_key={_scd2_incremental_unique_key(spec)},",
            "    on_schema_change='fail',",
        ]
    config_lines = _model_config_lines(
        materialized=materialized,
        schema=target["schema"],
        alias=table_name,
        database=target.get("database"),
        extra_config_lines=extra_config_lines,
    )
    if _change_type(spec) == "scd2":
        body_lines = _scd2_final_body_lines(
            spec,
            source_model,
            field_select_lines,
            audit_select_lines,
            quarantine_enabled=quarantine_enabled,
            fail_load_enabled=fail_load_enabled,
        )
    elif quarantine_enabled:
        select_lines = [*field_select_lines, *audit_select_lines]
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
        select_lines = [*field_select_lines, *audit_select_lines]
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
        select_lines = [*field_select_lines, *audit_select_lines]
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
        audit_select_lines,
        quarantine_enabled=quarantine_enabled,
        fail_load_enabled=fail_load_enabled,
    )
    sql = "\n".join(["{{", "  config(", *config_lines, "  )", "}}", "", *runtime_log_lines, *body_lines])
    _write(result.output_dir / "models" / "generated" / f"{target['id']}.sql", sql, result)
    if quarantine_enabled:
        _write_quarantine_model(spec, result)


def _audit_select_lines() -> list[str]:
    return [
        "    cast('{{ var(\"audit_data_process_key\", \"manual\") }}' "
        f"as {GENERATED_METADATA_FIELD_TYPES['audit_data_process_key']}) as AUDIT_DATA_PROCESS_KEY",
        f"    cast(current_timestamp() as {GENERATED_METADATA_FIELD_TYPES['audit_created_datetime']}) "
        "as AUDIT_CREATED_DATETIME",
        f"    cast(current_timestamp() as {GENERATED_METADATA_FIELD_TYPES['audit_last_changed_datetime']}) "
        "as AUDIT_LAST_CHANGED_DATETIME",
    ]


def _scd2_final_body_lines(
    spec: dict[str, Any],
    source_model: str,
    field_select_lines: list[str],
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
            audit_select_lines,
            quarantine_enabled=quarantine_enabled,
            fail_load_enabled=fail_load_enabled,
        )

    output_lines = [f"    {_quote_identifier(field['id'])}" for field in fields(spec)]
    output_lines.extend(
        [
            "    IS_CURRENT_FLAG",
            "    IS_DELETED_FLAG",
            "    VALID_FROM_DATETIME",
            "    VALID_TO_DATETIME",
            "    BUSINESS_DATA_HASH",
            *audit_select_lines,
        ]
    )
    return [
        *_scd2_valid_rows_lines(spec, source_model, quarantine_enabled, fail_load_enabled),
        "typed_rows as (",
        "    select",
        ",\n".join(
            [
                *field_select_lines,
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
        f"            then cast({_sql_string(SCD2_START_OF_TIME)} as timestamp_tz)",
        "            else TMS_VALID_FROM_DATETIME_CANDIDATE",
        "        end as VALID_FROM_DATETIME",
        "    from typed_rows",
        "),",
        "windowed_rows as (",
        "    select",
        "        *,",
        f"        {_valid_to_datetime_expression(spec)} as VALID_TO_DATETIME",
        "    from valid_from_rows",
        "),",
        "flagged_rows as (",
        "    select",
        "        *,",
        "        case",
        f"            when VALID_TO_DATETIME = cast({_sql_string(SCD2_END_OF_TIME)} as timestamp_tz)",
        "            then 'Y'",
        "            else 'N'",
        "        end as IS_CURRENT_FLAG,",
        f"        {_is_deleted_flag_expression(spec)} as IS_DELETED_FLAG",
        "    from windowed_rows",
        ")",
        "",
        "select",
        ",\n".join(output_lines),
        "from flagged_rows",
        "",
    ]


def _scd2_duplicate_hash_runtime_log_lines(
    spec: dict[str, Any],
    source_model: str,
    field_select_lines: list[str],
    audit_select_lines: list[str],
    *,
    quarantine_enabled: bool,
    fail_load_enabled: bool,
) -> list[str]:
    if not _scd2_uses_target_merge(spec):
        return []
    change_row_columns = _scd2_change_row_columns(spec)
    return [
        "{% if execute and is_incremental() %}",
        "{% set tms_scd2_duplicate_hash_log_sql %}",
        *_scd2_valid_rows_lines(spec, source_model, quarantine_enabled, fail_load_enabled),
        "typed_rows as (",
        "    select",
        ",\n".join(
            [
                *field_select_lines,
                f"    {_valid_from_datetime_expression(spec)} as TMS_VALID_FROM_DATETIME_CANDIDATE",
                f"    {_business_data_hash_expression(spec)} as BUSINESS_DATA_HASH",
                f"    {_is_deleted_flag_expression(spec)} as TMS_IS_DELETED_FLAG_CANDIDATE",
                "    cast(null as timestamp_tz) as TMS_EXISTING_VALID_TO_DATETIME",
                "    cast(null as varchar(1)) as TMS_EXISTING_IS_CURRENT_FLAG",
                "    'N' as TMS_IS_EXISTING_TARGET_ROW",
                *audit_select_lines,
            ]
        ),
        "    from valid_rows",
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
        "          and typed_rows.TMS_VALID_FROM_DATETIME_CANDIDATE >= current_target.VALID_FROM_DATETIME",
        "    )",
        "),",
        "incoming_key_rows as (",
        "    select distinct",
        ",\n".join(f"        {_quote_identifier(column)}" for column in _business_key_columns(spec)),
        "    from typed_rows",
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
        "          and typed_rows.TMS_VALID_FROM_DATETIME_CANDIDATE >= current_target.VALID_FROM_DATETIME",
        "    )",
        "),",
        "missing_from_source_delete_rows as (",
        "    select",
        ",\n".join(
            [
                *[
                    f"        current_target.{_quote_identifier(field['id'])} as {_quote_identifier(field['id'])}"
                    for field in fields(spec)
                ],
                f"        {_valid_from_datetime_expression(spec)} as TMS_VALID_FROM_DATETIME_CANDIDATE",
                "        current_target.BUSINESS_DATA_HASH as BUSINESS_DATA_HASH",
                "        'Y' as TMS_IS_DELETED_FLAG_CANDIDATE",
                "        cast(null as timestamp_tz) as TMS_EXISTING_VALID_TO_DATETIME",
                "        cast(null as varchar(1)) as TMS_EXISTING_IS_CURRENT_FLAG",
                "        'N' as TMS_IS_EXISTING_TARGET_ROW",
                *audit_select_lines,
            ]
        ),
        "    from current_target_rows as current_target",
        "    where coalesce(current_target.IS_DELETED_FLAG, 'N') <> 'Y'",
        "      and not exists (",
        "          select 1",
        "          from incoming_key_rows as incoming_key",
        f"          where {_business_key_join_condition(spec, 'current_target', 'incoming_key')}",
        "      )",
        "),",
        "change_rows as (",
        "    select",
        ",\n".join(f"        {column}" for column in change_row_columns),
        "    from source_change_rows",
        "    union all",
        "    select",
        ",\n".join(f"        {column}" for column in change_row_columns),
        "    from missing_from_source_delete_rows",
        "),",
        "affected_key_rows as (",
        "    select distinct",
        ",\n".join(f"        {_quote_identifier(column)}" for column in _business_key_columns(spec)),
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
                "        existing_target.VALID_FROM_DATETIME as TMS_VALID_FROM_DATETIME_CANDIDATE",
                "        existing_target.BUSINESS_DATA_HASH as BUSINESS_DATA_HASH",
                "        existing_target.IS_DELETED_FLAG as TMS_IS_DELETED_FLAG_CANDIDATE",
                "        existing_target.VALID_TO_DATETIME as TMS_EXISTING_VALID_TO_DATETIME",
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
        "                order by case when TMS_IS_EXISTING_TARGET_ROW = 'N' then 0 else 1 end",
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
        "{% if tms_historical_duplicate_skip_count > 0 and " + _sql_string(_business_data_hash_duplicate_mode(spec)) + " == 'skip' %}{{ log('SCD2 duplicate hash handling: historical duplicate rows skipped=' ~ tms_historical_duplicate_skip_count, info=true) }}{% endif %}",
        "{% if tms_historical_boundary_update_count > 0 and " + _sql_string(_business_data_hash_duplicate_mode(spec)) + " == 'update' %}{{ log('SCD2 duplicate hash handling: historical duplicate boundaries updated=' ~ tms_historical_boundary_update_count, info=true) }}{% endif %}",
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
        "    from {{ this }}",
        "),",
    ]


def _scd2_target_merge_body_lines(
    spec: dict[str, Any],
    source_model: str,
    field_select_lines: list[str],
    audit_select_lines: list[str],
    *,
    quarantine_enabled: bool,
    fail_load_enabled: bool,
) -> list[str]:
    output_lines = [f"    {_quote_identifier(field['id'])}" for field in fields(spec)]
    output_lines.extend(
        [
            "    IS_CURRENT_FLAG",
            "    IS_DELETED_FLAG",
            "    VALID_FROM_DATETIME",
            "    VALID_TO_DATETIME",
            "    BUSINESS_DATA_HASH",
            "    AUDIT_DATA_PROCESS_KEY",
            "    AUDIT_CREATED_DATETIME",
            "    AUDIT_LAST_CHANGED_DATETIME",
        ]
    )
    change_row_columns = _scd2_change_row_columns(spec)
    return [
        *_scd2_valid_rows_lines(spec, source_model, quarantine_enabled, fail_load_enabled),
        "typed_rows as (",
        "    select",
        ",\n".join(
            [
                *field_select_lines,
                f"    {_valid_from_datetime_expression(spec)} as TMS_VALID_FROM_DATETIME_CANDIDATE",
                f"    {_business_data_hash_expression(spec)} as BUSINESS_DATA_HASH",
                f"    {_is_deleted_flag_expression(spec)} as TMS_IS_DELETED_FLAG_CANDIDATE",
                "    cast(null as timestamp_tz) as TMS_EXISTING_VALID_TO_DATETIME",
                "    cast(null as varchar(1)) as TMS_EXISTING_IS_CURRENT_FLAG",
                "    'N' as TMS_IS_EXISTING_TARGET_ROW",
                *audit_select_lines,
            ]
        ),
        "    from valid_rows",
        "),",
        *_scd2_existing_target_rows_lines(spec),
        "current_target_rows as (",
        "    select *",
        "    from existing_target_rows",
        "    where IS_CURRENT_FLAG = 'Y'",
        "),",
        "incoming_key_rows as (",
        "    select distinct",
        ",\n".join(f"        {_quote_identifier(column)}" for column in _business_key_columns(spec)),
        "    from typed_rows",
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
        "          and typed_rows.TMS_VALID_FROM_DATETIME_CANDIDATE >= current_target.VALID_FROM_DATETIME",
        "    )",
        "),",
        "missing_from_source_delete_rows as (",
        "    select",
        ",\n".join(
            [
                *[
                    f"        current_target.{_quote_identifier(field['id'])} as {_quote_identifier(field['id'])}"
                    for field in fields(spec)
                ],
                f"        {_valid_from_datetime_expression(spec)} as TMS_VALID_FROM_DATETIME_CANDIDATE",
                "        current_target.BUSINESS_DATA_HASH as BUSINESS_DATA_HASH",
                "        'Y' as TMS_IS_DELETED_FLAG_CANDIDATE",
                "        cast(null as timestamp_tz) as TMS_EXISTING_VALID_TO_DATETIME",
                "        cast(null as varchar(1)) as TMS_EXISTING_IS_CURRENT_FLAG",
                "        'N' as TMS_IS_EXISTING_TARGET_ROW",
                *audit_select_lines,
            ]
        ),
        "    from current_target_rows as current_target",
        "    where coalesce(current_target.IS_DELETED_FLAG, 'N') <> 'Y'",
        "      and not exists (",
        "          select 1",
        "          from incoming_key_rows as incoming_key",
        f"          where {_business_key_join_condition(spec, 'current_target', 'incoming_key')}",
        "      )",
        "),",
        "change_rows as (",
        "    select",
        ",\n".join(f"        {column}" for column in change_row_columns),
        "    from source_change_rows",
        "    union all",
        "    select",
        ",\n".join(f"        {column}" for column in change_row_columns),
        "    from missing_from_source_delete_rows",
        "),",
        "affected_key_rows as (",
        "    select distinct",
        ",\n".join(f"        {_quote_identifier(column)}" for column in _business_key_columns(spec)),
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
                "        existing_target.VALID_FROM_DATETIME as TMS_VALID_FROM_DATETIME_CANDIDATE",
                "        existing_target.BUSINESS_DATA_HASH as BUSINESS_DATA_HASH",
                "        existing_target.IS_DELETED_FLAG as TMS_IS_DELETED_FLAG_CANDIDATE",
                "        existing_target.VALID_TO_DATETIME as TMS_EXISTING_VALID_TO_DATETIME",
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
        "                order by case when TMS_IS_EXISTING_TARGET_ROW = 'N' then 0 else 1 end",
        "            ) as TMS_VERSION_ROW_NUMBER",
        "        from version_row_candidates",
        "    )",
        "    where TMS_VERSION_ROW_NUMBER = 1",
        "),",
        *_scd2_duplicate_boundary_lines(spec),
        *_scd2_window_and_flag_lines(spec, "deduplicated_version_rows"),
        "",
        "select",
        ",\n".join(output_lines),
        "from flagged_rows",
        "",
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
    return _change_type(spec) == "scd2" and _delete_detection_mode(spec) == "missing_from_source"


def _scd2_incremental_unique_key(spec: dict[str, Any]) -> str:
    columns = _business_key_columns(spec)
    return "[" + ", ".join(_sql_string(_physical_name(column)) for column in columns) + "]"


def _scd2_target_column_types(spec: dict[str, Any]) -> list[tuple[str, str]]:
    columns = [
        (str(field["id"]), str(field["data_type"]))
        for field in fields(spec)
    ]
    columns.extend(
        [
            ("IS_CURRENT_FLAG", GENERATED_METADATA_FIELD_TYPES["is_current_flag"]),
            ("IS_DELETED_FLAG", GENERATED_METADATA_FIELD_TYPES["is_deleted_flag"]),
            ("VALID_FROM_DATETIME", GENERATED_METADATA_FIELD_TYPES["valid_from_datetime"]),
            ("VALID_TO_DATETIME", GENERATED_METADATA_FIELD_TYPES["valid_to_datetime"]),
            ("BUSINESS_DATA_HASH", GENERATED_METADATA_FIELD_TYPES["business_data_hash"]),
            ("AUDIT_DATA_PROCESS_KEY", GENERATED_METADATA_FIELD_TYPES["audit_data_process_key"]),
            ("AUDIT_CREATED_DATETIME", GENERATED_METADATA_FIELD_TYPES["audit_created_datetime"]),
            ("AUDIT_LAST_CHANGED_DATETIME", GENERATED_METADATA_FIELD_TYPES["audit_last_changed_datetime"]),
        ]
    )
    return columns


def _scd2_existing_target_rows_lines(spec: dict[str, Any]) -> list[str]:
    columns = _scd2_target_column_types(spec)
    return [
        "{% if is_incremental() %}",
        "existing_target_rows as (",
        "    select",
        ",\n".join(f"        {_quote_identifier(column)}" for column, _ in columns),
        "    from {{ this }}",
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
        *[_quote_identifier(field["id"]) for field in fields(spec)],
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
    mode = _business_data_hash_duplicate_mode(spec)
    previous_matches = _scd2_previous_duplicate_condition()
    next_matches = _scd2_next_duplicate_condition()
    if mode == "update":
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
    return (
        "not ("
        "TMS_IS_EXISTING_TARGET_ROW = 'N' "
        f"and ({previous_matches} or {next_matches})"
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
        f"{left_alias}.{_quote_identifier(column)} = {right_alias}.{_quote_identifier(column)}"
        for column in _business_key_columns(spec)
    )


def _scd2_version_row_key_columns(spec: dict[str, Any]) -> str:
    columns = [*_business_key_columns(spec), "TMS_VALID_FROM_DATETIME_CANDIDATE"]
    return ", ".join(_quote_identifier(column) for column in columns)


def _scd2_window_and_flag_lines(spec: dict[str, Any], input_cte: str) -> list[str]:
    return [
        "valid_from_rows as (",
        "    select",
        "        *,",
        "        case",
        f"            when row_number() over ({_scd2_window_clause(spec, 'TMS_VALID_FROM_DATETIME_CANDIDATE')}) = 1",
        f"            then cast({_sql_string(SCD2_START_OF_TIME)} as timestamp_tz)",
        "            else TMS_VALID_FROM_DATETIME_CANDIDATE",
        "        end as VALID_FROM_DATETIME",
        f"    from {input_cte}",
        "),",
        "windowed_rows as (",
        "    select",
        "        *,",
        f"        {_valid_to_datetime_expression(spec)} as VALID_TO_DATETIME",
        "    from valid_from_rows",
        "),",
        "flagged_rows as (",
        "    select",
        "        *,",
        "        case",
        f"            when VALID_TO_DATETIME = cast({_sql_string(SCD2_END_OF_TIME)} as timestamp_tz)",
        "            then 'Y'",
        "            else 'N'",
        "        end as IS_CURRENT_FLAG,",
        "        TMS_IS_DELETED_FLAG_CANDIDATE as IS_DELETED_FLAG",
        "    from windowed_rows",
        ")",
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
        schema=relation.schema,
        alias=alias,
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
            "    select count(*) as FAILURE_COUNT",
            "    from failed_validation_rows",
            ")",
            "",
            "select 0 as VALIDATION_FAILURE_GUARD",
            "from failure_count",
            "where FAILURE_COUNT = 0",
            "union all",
            "select cast('TYPE_MATERIALISATION_VALIDATION_FAILED' as number) as VALIDATION_FAILURE_GUARD",
            "from failure_count",
            "where FAILURE_COUNT > 0",
            "",
        ]
    )
    _write(result.output_dir / "models" / "generated" / f"{model_name}.sql", sql, result)


def _model_config_lines(
    *,
    materialized: str,
    schema: str,
    alias: str,
    database: str | None,
    extra_config_lines: list[str] | None = None,
) -> list[str]:
    config_lines = [
        f"    materialized='{materialized}',",
        f"    schema='{_physical_name(schema)}',",
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
        "    cast(current_timestamp() as timestamp_tz) as LOADED_AT",
        f"    {_job_id_expression()} as JOB_ID",
        "    FAILURE_DETAILS",
    ]
    select_lines.extend(f"    {_quote_identifier(column)}" for column in source_columns)
    config_lines = _model_config_lines(
        materialized="incremental",
        database=quarantine.database,
        schema=quarantine.schema,
        alias=quarantine.table,
        extra_config_lines=[
            "    incremental_strategy='append',",
            "    on_schema_change='append_new_columns',",
        ],
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


def _write_generated_macros(output_dir: Path, macros: dict[str, str], result: DbtGenerationResult) -> None:
    _write(output_dir / "macros" / "generated" / "create_schema.sql", _create_schema_macro(), result)
    _write(output_dir / "macros" / "generated" / "generate_schema_name.sql", _generate_schema_name_macro(), result)
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
            "    {%- set override_schema = var('target_schema', none) -%}",
            "    {%- if override_schema is not none -%}",
            "        {{ override_schema | trim | upper }}",
            "    {%- elif custom_schema_name is none -%}",
            "        {{ target.schema | upper }}",
            "    {%- else -%}",
            "        {{ custom_schema_name | trim | upper }}",
            "    {%- endif -%}",
            "{%- endmacro %}",
            "",
        ]
    )


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
    if _failure_mode(spec) == "fail_load":
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
    }
    if _scd2_uses_target_merge(spec):
        unit_test["overrides"] = _unit_test_non_incremental_overrides()
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
            }
        )
    data = {"unit_tests": unit_tests}
    content = yaml.safe_dump(data, sort_keys=False)
    _write(result.output_dir / "models" / "generated" / f"{target['id']}_unit_tests.yml", content, result)


def _unit_test_validation_guard_fixture_sql() -> str:
    return "\n".join(
        [
            "select",
            "    cast(0 as number) as VALIDATION_FAILURE_GUARD",
        ]
    )


def _unit_test_non_incremental_overrides() -> dict[str, dict[str, bool]]:
    return {"macros": {"is_incremental": False}}


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
            if _change_type(spec) == "scd2":
                hash_value = _unit_test_business_data_hash(spec, transformed_values, warnings)
                if hash_value is not _SKIP_EXPECTED:
                    expected_row["BUSINESS_DATA_HASH"] = hash_value
            source_rows.append(source_row)
            expected_rows.append(expected_row)
            transformed_rows.append(transformed_values)
    if _change_type(spec) == "scd2":
        _apply_unit_test_scd2_validity(spec, expected_rows, transformed_rows, warnings)
    return source_rows, expected_rows, warnings


_SKIP_EXPECTED = object()


def _extract_csv_value(field: dict[str, Any], row: list[str], header: list[str] | None) -> str | None:
    source = field.get("source", {})
    pos = source.get("pos")
    if isinstance(pos, int):
        return row[pos] if pos < len(row) else None
    column = source.get("column")
    if isinstance(column, str) and header is not None and column in header:
        index = header.index(column)
        return row[index] if index < len(row) else None
    return None


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
    hash_parts: list[str] = []
    for field in _business_data_hash_fields(spec):
        field_id = case_key(field["id"])
        if field_id not in transformed_values:
            warnings.append(
                Diagnostic(
                    "BUSINESS_DATA_HASH omitted from unit-test expectation because a hash field is not implemented locally",
                    field["id"],
                )
            )
            return _SKIP_EXPECTED
        value = transformed_values[field_id]
        hash_parts.append("" if value is None else str(value))
    return hashlib.sha256("|".join(hash_parts).encode("utf-8")).hexdigest()


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
        expected_row["VALID_FROM_DATETIME"] = _format_unit_test_datetime(valid_from_value)
    for expected_row, valid_to_value in zip(expected_rows, valid_to_values, strict=True):
        expected_row["VALID_TO_DATETIME"] = _format_unit_test_datetime(valid_to_value)
        expected_row["IS_CURRENT_FLAG"] = "Y" if _format_unit_test_datetime(valid_to_value) == SCD2_END_OF_TIME else "N"


def _unit_test_valid_from_datetime(
    spec: dict[str, Any],
    transformed_values: dict[str, Any],
    warnings: list[Diagnostic],
) -> datetime | object:
    config = _valid_from_datetime_config(spec)
    selection = config.get("valid_from_datetime_selection", "load_datetime")
    if selection == "field":
        field_id = case_key(str(config["field"]))
        if field_id not in transformed_values:
            warnings.append(Diagnostic("VALID_FROM_DATETIME omitted from unit-test expectation because field is not implemented locally", str(config["field"])))
            return _SKIP_EXPECTED
        value = transformed_values[field_id]
    elif selection == "explicit":
        value = config["value"]
    else:
        warnings.append(Diagnostic("VALID_FROM_DATETIME omitted from unit-test expectation because load_datetime is runtime-defined", "$.control_data.scd.valid_from_datetime"))
        return _SKIP_EXPECTED
    parsed = _parse_unit_test_datetime(value)
    if config.get("truncate_to_day") is True:
        parsed = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
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
    config = _valid_to_datetime_config(spec)
    selection = config.get("valid_to_datetime_selection", "next")
    if selection == "field":
        values: list[datetime] = []
        for transformed_values in transformed_rows:
            field_id = case_key(str(config["field"]))
            if field_id not in transformed_values:
                warnings.append(Diagnostic("VALID_TO_DATETIME omitted from unit-test expectation because field is not implemented locally", str(config["field"])))
                return _SKIP_EXPECTED, valid_from_values
            values.append(_parse_unit_test_datetime(transformed_values[field_id]))
        return values, valid_from_values
    if selection == "explicit":
        return [_parse_unit_test_datetime(config["value"]) for _ in transformed_rows], valid_from_values

    adjusted_valid_from = _continuous_unit_test_valid_from_datetimes(spec, transformed_rows, valid_from_values)
    return _continuous_unit_test_valid_to_datetimes(spec, transformed_rows, adjusted_valid_from), adjusted_valid_from


def _continuous_unit_test_valid_from_datetimes(
    spec: dict[str, Any],
    transformed_rows: list[dict[str, Any]],
    valid_from_values: list[datetime],
) -> list[datetime]:
    adjusted = list(valid_from_values)
    for group in _unit_test_business_key_groups(spec, transformed_rows, adjusted):
        first_index = group[0]
        adjusted[first_index] = _parse_unit_test_datetime(SCD2_START_OF_TIME)
    return adjusted


def _continuous_unit_test_valid_to_datetimes(
    spec: dict[str, Any],
    transformed_rows: list[dict[str, Any]],
    valid_from_values: list[datetime],
) -> list[datetime]:
    valid_to_values = [_parse_unit_test_datetime(SCD2_END_OF_TIME) for _ in valid_from_values]
    for group in _unit_test_business_key_groups(spec, transformed_rows, valid_from_values):
        for current_index, next_index in zip(group, group[1:]):
            valid_to_values[current_index] = valid_from_values[next_index]
    return valid_to_values


def _unit_test_business_key_groups(
    spec: dict[str, Any],
    transformed_rows: list[dict[str, Any]],
    valid_from_values: list[datetime],
) -> list[list[int]]:
    groups: dict[tuple[Any, ...], list[int]] = {}
    for index, transformed_values in enumerate(transformed_rows):
        key = tuple(transformed_values.get(case_key(column)) for column in _business_key_columns(spec))
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
    return (
        "case "
        f"when {input_expression} is not null and {parsed_expression} is null "
        f"then {_sql_string(f'field `{field_id}` does not match {transform_type} format `{transform['format']}`')} "
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
        return (
            f"try_to_timestamp_tz(concat(cast({expression} as varchar), ' +0000'), "
            f"{_sql_string(snowflake_format + ' TZHTZM')})"
        )
    if timezone_if_missing == "local":
        return f"to_timestamp_tz(try_to_timestamp_ntz(cast({expression} as varchar), {_sql_string(snowflake_format)}))"
    if not _python_datetime_format_has_timezone(python_format):
        raise ValueError(
            "`parse_timestamp` dbt generation requires a timezone directive in `format` "
            "or `timezone_if_missing`"
        )
    return f"try_to_timestamp_tz(cast({expression} as varchar), {_sql_string(snowflake_format)})"


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
            failures.append(
                "case "
                f"when {expression} is not null and length({expression}) < {int(rule['value'])} "
                f"then {_sql_string(f'field `{field_id}` length is less than {rule['value']}')} "
                "end"
            )
        elif rule_type == "max_length":
            failures.append(
                "case "
                f"when {expression} is not null and length({expression}) > {int(rule['value'])} "
                f"then {_sql_string(f'field `{field_id}` length is greater than {rule['value']}')} "
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
            failures.append(
                "case "
                f"when {expression} is not null and {numeric_expression} < {rule['value']} "
                f"then {_sql_string(f'field `{field_id}` is less than {rule['value']}')} "
                "end"
            )
        elif rule_type == "max_value":
            numeric_expression = _try_cast_expression(expression, "number")
            failures.append(
                "case "
                f"when {expression} is not null and {numeric_expression} > {rule['value']} "
                f"then {_sql_string(f'field `{field_id}` is greater than {rule['value']}')} "
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
    suffix = "," if trailing_comma else ""
    return [
        "validation_rows as (",
        "    select",
        "        *,",
        f"        {_failure_details_expression(spec)} as FAILURE_DETAILS",
        "    from source_rows",
        f"){suffix}",
    ]


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


def _valid_from_to_mode(spec: dict[str, Any]) -> str:
    return str(_scd_config(spec).get("valid_from_to_mode", "continuous"))


def _business_data_hash_duplicate_mode(spec: dict[str, Any]) -> str:
    return str(_scd_config(spec).get("business_data_hash_duplicate_mode", "skip"))


def _valid_from_datetime_config(spec: dict[str, Any]) -> dict[str, Any]:
    config = _scd_config(spec).get("valid_from_datetime", {})
    return config if isinstance(config, dict) else {}


def _valid_to_datetime_config(spec: dict[str, Any]) -> dict[str, Any]:
    config = _scd_config(spec).get("valid_to_datetime", {})
    return config if isinstance(config, dict) else {}


def _delete_detection_config(spec: dict[str, Any]) -> dict[str, Any]:
    config = _scd_config(spec).get("delete_detection", {})
    return config if isinstance(config, dict) else {"mode": "never"}


def _delete_detection_mode(spec: dict[str, Any]) -> str:
    return str(_delete_detection_config(spec).get("mode", "never"))


def _scd1_delete_filter_lines(spec: dict[str, Any]) -> list[str]:
    if _change_type(spec) != "scd1" or _delete_detection_mode(spec) != "field":
        return []
    return [f"where not ({_delete_detection_field_condition(spec)})"]


def _is_deleted_flag_expression(spec: dict[str, Any]) -> str:
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
    selection = config.get("valid_from_datetime_selection", "load_datetime")
    if selection == "field":
        field = _field_by_id(spec, str(config["field"]))
        expression = f"cast({_field_expression(field)} as timestamp_tz)"
    elif selection == "explicit":
        expression = f"cast({_sql_scalar(config['value'])} as timestamp_tz)"
    else:
        expression = "cast('{{ run_started_at }}' as timestamp_tz)"
    if config.get("truncate_to_day") is True:
        expression = f"date_trunc('day', {expression})"
    return expression


def _valid_to_datetime_expression(spec: dict[str, Any]) -> str:
    config = _valid_to_datetime_config(spec)
    selection = config.get("valid_to_datetime_selection", "next")
    if selection == "field":
        return f"cast({_quote_identifier(str(config['field']))} as timestamp_tz)"
    if selection == "explicit":
        return f"cast({_sql_scalar(config['value'])} as timestamp_tz)"
    return (
        "coalesce("
        f"lead(VALID_FROM_DATETIME) over ({_scd2_window_clause(spec, 'VALID_FROM_DATETIME')})"
        ", "
        f"cast({_sql_string(SCD2_END_OF_TIME)} as timestamp_tz)"
        ")"
    )


def _scd2_window_clause(spec: dict[str, Any], order_column: str) -> str:
    partition_columns = ", ".join(_quote_identifier(column) for column in _business_key_columns(spec))
    return f"partition by {partition_columns} order by {order_column}"


def _business_key_columns(spec: dict[str, Any]) -> list[str]:
    business_key = _scd_config(spec).get("business_key", [])
    return [str(column) for column in business_key if isinstance(column, str)]


def _field_by_id(spec: dict[str, Any], field_id: str) -> dict[str, Any]:
    key = case_key(field_id)
    for field in fields(spec):
        if isinstance(field, dict) and isinstance(field.get("id"), str) and case_key(field["id"]) == key:
            return field
    raise ValueError(f"field `{field_id}` does not exist")


def _business_data_hash_expression(spec: dict[str, Any]) -> str:
    hash_fields = _business_data_hash_fields(spec)
    if not hash_fields:
        value_expression = "''"
    else:
        hash_values = [
            "coalesce("
            f"cast(cast({_field_expression(field)} as {field['data_type']}) as varchar), "
            "''"
            ")"
            for field in hash_fields
        ]
        value_expression = "concat_ws('|', " + ", ".join(hash_values) + ")"
    return (
        f"cast(sha2({value_expression}, 256) "
        f"as {GENERATED_METADATA_FIELD_TYPES['business_data_hash']})"
    )


def _business_data_hash_fields(spec: dict[str, Any]) -> list[dict[str, Any]]:
    target_fields = fields(spec)
    fields_by_id = {
        case_key(field["id"]): field
        for field in target_fields
        if isinstance(field, dict) and isinstance(field.get("id"), str)
    }
    control_data = spec.get("control_data", {})
    if not isinstance(control_data, dict):
        control_data = {}
    scd = control_data.get("scd", {})
    if not isinstance(scd, dict):
        scd = {}
    hash_config = scd.get("business_data_hash", {})
    if not isinstance(hash_config, dict):
        hash_config = {}
    mode = hash_config.get("mode", "exclude")
    configured_fields = [
        case_key(field_id)
        for field_id in hash_config.get("fields", [])
        if isinstance(field_id, str)
    ]
    if mode == "include":
        return [
            fields_by_id[field_id]
            for field_id in configured_fields
            if field_id in fields_by_id
        ]
    excluded = set(configured_fields)
    return [
        field
        for field in target_fields
        if isinstance(field, dict)
        and isinstance(field.get("id"), str)
        and case_key(field["id"]) not in excluded
    ]


def _source_model_name(target_id: str) -> str:
    return f"{target_id}__source"


def _quarantine_model_name(target_id: str) -> str:
    return f"{target_id}__quarantine"


def _validation_guard_model_name(target_id: str) -> str:
    return f"{target_id}__validation_guard"


def _csv_load_method(source: dict[str, Any]) -> str:
    return str(source.get("load_method", "stage"))


def _csv_seed_name(spec: dict[str, Any]) -> str:
    seed = spec["source"].get("seed", {})
    if not isinstance(seed, dict):
        seed = {}
    return str(seed.get("name", f"{spec['target']['id']}__seed"))


def _csv_seed_file_path(spec: dict[str, Any], spec_path: Path) -> Path:
    seed = spec["source"].get("seed", {})
    if not isinstance(seed, dict):
        seed = {}
    raw_path = seed.get("file")
    if not isinstance(raw_path, str):
        raise OSError("source.seed.file is required when source.load_method is dbt_seed")
    path = Path(raw_path)
    if not path.is_absolute():
        path = spec_path.parent / path
    if not path.exists():
        raise OSError(f"seed CSV file does not exist: {path}")
    return path


def _seed_project_config(spec: dict[str, Any]) -> dict[str, Any] | None:
    source = spec.get("source", {})
    if not isinstance(source, dict) or source.get("format") != "csv" or _csv_load_method(source) != "dbt_seed":
        return None

    target = spec["target"]
    seed = source.get("seed", {})
    if not isinstance(seed, dict):
        seed = {}
    seed_name = _csv_seed_name(spec)
    config: dict[str, Any] = {
        "+quote_columns": False,
        "+schema": _physical_name(seed.get("schema", target["schema"])),
        "+alias": _physical_name(seed_name),
        "+column_types": {
            _physical_name(column): "varchar"
            for column in _source_output_columns(spec)
        },
    }
    if seed.get("database"):
        config["+database"] = _physical_name(seed["database"])
    if source.get("delimiter", ",") != ",":
        config["+delimiter"] = source["delimiter"]
    return {DBT_PROJECT_NAME: {seed_name: config}}


def _csv_stage_location(source: dict[str, Any]) -> str:
    location = source.get("location", {})
    if not isinstance(location, dict):
        location = {}
    database = location.get("database")
    schema = location.get("schema", "AD_HOC")
    stage = location.get("stage", "@csv_stage")
    filename = location.get("filename")
    parts = []
    if database:
        parts.append(_physical_name(database))
    if schema:
        parts.append(_physical_name(schema))
    stage_text = str(stage)
    if stage_text.startswith("@"):
        stage_text = stage_text[1:]
    parts.append(_physical_name(stage_text))
    relation = ".".join(parts)
    if filename:
        relation = f"{relation}/{filename}"
    return _stage_reference(relation)


def _table_source_relation(source: dict[str, Any]) -> str:
    parts = []
    if source.get("database"):
        parts.append(_physical_name(source["database"]))
    parts.extend([_physical_name(source["schema"]), _physical_name(source["table"])])
    return ".".join(parts)


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


def _quarantine_enabled(spec: dict[str, Any]) -> bool:
    control_data = spec.get("control_data", {})
    return isinstance(control_data, dict) and control_data.get("failure_mode") == "quarantine_row"


def _failure_mode(spec: dict[str, Any]) -> str:
    control_data = spec.get("control_data", {})
    if not isinstance(control_data, dict):
        return "fail_load"
    return str(control_data.get("failure_mode", "fail_load"))


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
        schema=_physical_name(quarantine["schema"]) if quarantine.get("schema") else target.schema,
        table=_physical_name(quarantine.get("table", f"{target.table}__QUARANTINE")),
    )


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


def _source_output_columns(spec: dict[str, Any]) -> list[str]:
    target_fields = fields(spec)
    if spec["source"]["format"] == "csv":
        target_fields = sorted(
            target_fields,
            key=lambda field: field.get("source", {}).get("pos", 999999),
        )
    return [_source_column_name(field) for field in target_fields]


def _source_column_name(field: dict[str, Any]) -> str:
    source = field.get("source", {})
    column = source.get("column")
    if isinstance(column, str):
        return _physical_name(column)
    pos = source.get("pos")
    if isinstance(pos, int):
        return _position_column_name(pos)
    return _physical_name(field["id"])


def _position_column_name(pos: int) -> str:
    return f"COL_{pos}"


def _job_hooks(spec: dict[str, Any], spec_file_name: str) -> tuple[list[str], list[str]]:
    relation = _relation_name(_job_relation_config(spec))
    target_relation = _target_relation_config(spec)
    generated_table = _runtime_relation_label(target_relation, schema_var="target_schema")
    quarantine_table = (
        _runtime_relation_label(_quarantine_relation_config(spec), schema_var="target_schema")
        if _quarantine_enabled(spec)
        else None
    )
    generated_relation_lookup = _dbt_relation_lookup(
        target_relation,
        "tms_generated_relation",
        schema_var="target_schema",
    )
    quarantine_relation_lookup = (
        _dbt_relation_lookup(
            _quarantine_relation_config(spec),
            "tms_quarantine_relation",
            schema_var="target_schema",
        )
        if _quarantine_enabled(spec)
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
    return (
        [_optional_job_hook(create_sql), _optional_job_hook(start_sql)],
        [_optional_job_hook(create_sql), _optional_job_hook(end_sql)],
    )


def _optional_job_hook(sql: str) -> str:
    return "{% if var('tms_enable_job_hooks', true) %}" + sql + "{% endif %}"


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return _sql_string(str(value))


def _nullable_sql_string(value: str | None) -> str:
    if value is None:
        return "cast(null as varchar(1024))"
    return f"cast({_sql_string(value)} as varchar(1024))"


def _sql_literal(value: Any, data_type: str) -> str:
    if value is None:
        return f"cast(null as {data_type})"
    return f"cast({_sql_string(str(value))} as {data_type})"


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
