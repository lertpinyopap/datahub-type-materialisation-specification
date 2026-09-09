"""Generation of source and seed models."""
from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path
from typing import Any

from .errors import Diagnostic
from .source_files import csv_load_method, csv_seed_file_path
from .spec import case_key, fields
DBT_PROJECT_NAME = "type_materialisation_generated"

from .dbt_sql import (
    _indent_sql, _macro_object_name, _physical_name, _quote_identifier,
    _sql_scalar, _sql_string, _stage_reference, _staging_schema,
    _staging_schema_config_expression,
    _target_relation_config,
)


def _write(path: Path, content: str, result: Any) -> None:
    path.write_text(content, encoding="utf-8")
    result.files.append(path)


def _model_config_lines(*, materialized: str, schema: str, alias: str, database: str | None, extra_config_lines: list[str] | None = None, schema_is_expression: bool = False) -> list[str]:
    """Use the central configuration renderer without importing it during module load."""
    from .dbt_generate import _model_config_lines as renderer
    return renderer(materialized=materialized, schema=schema, alias=alias, database=database, extra_config_lines=extra_config_lines, schema_is_expression=schema_is_expression)


def _scd2_derived_source_helper_lines(spec: dict[str, Any]) -> list[str]:
    """Delegate SCD-specific source projection until SCD generation is extracted."""
    from .dbt_generate import _scd2_derived_source_helper_lines as renderer
    return renderer(spec)

def _write_source_model(spec: dict[str, Any], options: GenerateDbtOptions, result: DbtGenerationResult) -> None:
    source = spec["source"]
    if source["format"] == "table":
        _write_table_source_model(spec, options, result)
    else:
        _write_csv_source_model(spec, options, result)



def _write_csv_source_model(spec: dict[str, Any], options: GenerateDbtOptions, result: DbtGenerationResult) -> None:
    if csv_load_method(spec["source"]) == "dbt_seed":
        _write_csv_seed_source_model(spec, options, result)
        return

    target = spec["target"]
    model_name = _source_model_name(target["id"])
    stage = _stage_reference(options.csv_stage) if options.csv_stage else _csv_stage_location(spec["source"])
    source_query_select_lines = []
    seen_source_query_columns: set[str] = set()
    for field in fields(spec):
        source = field.get("source", {})
        if "fixed_value" in source:
            continue
        if isinstance(source.get("macro"), str):
            helper_column = _macro_helper_source_column_name(field)
            if helper_column is None:
                continue
            helper_key = case_key(helper_column)
            if helper_key in seen_source_query_columns:
                continue
            seen_source_query_columns.add(helper_key)
            pos = source.get("pos")
            if isinstance(pos, int):
                ordinal = pos + 1
                expression = _apply_default_value_expression(f"${ordinal}::string", source)
                source_query_select_lines.append(
                    f"        {expression} as {_quote_identifier(helper_column)}"
                )
            continue
        column = _source_column_name(field)
        column_key = case_key(column)
        if column_key in seen_source_query_columns:
            continue
        seen_source_query_columns.add(column_key)
        pos = source.get("pos")
        ordinal = int(pos) + 1
        expression = _apply_default_value_expression(f"${ordinal}::string", source)
        source_query_select_lines.append(f"        {expression} as {_quote_identifier(column)}")
    select_lines = []
    for field in fields(spec):
        source = field.get("source", {})
        if isinstance(source.get("macro"), str):
            expression = _source_macro_field_expression(field)
            output_column = str(field["id"])
        elif "fixed_value" in source:
            expression = _fixed_value_expression(source["fixed_value"])
            output_column = _source_column_name(field)
        else:
            column = _source_column_name(field)
            expression = f"source_query.{_quote_identifier(column)}"
            output_column = column
        select_lines.append(f"    {expression} as {_quote_identifier(output_column)}")
    sql = "\n".join(
        [
            "{{",
            "  config(",
            *_model_config_lines(
                materialized="view",
                database=_target_relation_config(spec).database,
                schema=_staging_schema_config_expression(spec),
                alias=model_name,
                schema_is_expression=True,
            ),
            "  )",
            "}}",
            "",
            "with source_query as (",
            "    select",
            ",\n".join(source_query_select_lines) if source_query_select_lines else "        1 as _DUMMY_SOURCE_COLUMN",
            f"    from {stage}",
            ")",
            "",
            "select",
            ",\n".join(select_lines),
            "from source_query as source_query",
            *_source_macro_join_lines(spec),
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

    source_query_select_lines = []
    seen_source_query_columns: set[str] = set()
    for field in fields(spec):
        source = field.get("source", {})
        if "fixed_value" in source:
            continue
        if isinstance(source.get("macro"), str):
            helper_column = _macro_helper_source_column_name(field)
            if helper_column is None:
                continue
            helper_key = case_key(helper_column)
            if helper_key in seen_source_query_columns:
                continue
            seen_source_query_columns.add(helper_key)
            expression = _apply_default_value_expression(
                f"cast({_quote_identifier(helper_column)} as string)",
                source,
            )
            source_query_select_lines.append(
                f"        {expression} as {_quote_identifier(helper_column)}"
            )
            continue
        column = _source_column_name(field)
        column_key = case_key(column)
        if column_key in seen_source_query_columns:
            continue
        seen_source_query_columns.add(column_key)
        expression = _apply_default_value_expression(f"cast({_quote_identifier(column)} as string)", source)
        source_query_select_lines.append(f"        {expression} as {_quote_identifier(column)}")
    select_lines = []
    for field in fields(spec):
        source = field.get("source", {})
        if isinstance(source.get("macro"), str):
            expression = _source_macro_field_expression(field)
            output_column = str(field["id"])
        elif "fixed_value" in source:
            expression = _fixed_value_expression(source["fixed_value"])
            output_column = _source_column_name(field)
        else:
            column = _source_column_name(field)
            expression = f"source_query.{_quote_identifier(column)}"
            output_column = column
        select_lines.append(f"    {expression} as {_quote_identifier(output_column)}")
    sql = "\n".join(
        [
            "{{",
            "  config(",
            *_model_config_lines(
                materialized="view",
                database=_target_relation_config(spec).database,
                schema=_staging_schema_config_expression(spec),
                alias=model_name,
                schema_is_expression=True,
            ),
            "  )",
            "}}",
            "",
            "with source_query as (",
            "    select",
            ",\n".join(source_query_select_lines) if source_query_select_lines else "        1 as _DUMMY_SOURCE_COLUMN",
            f"    from {{{{ ref('{seed_name}') }}}}",
            ")",
            "",
            "select",
            ",\n".join(select_lines),
            "from source_query as source_query",
            *_source_macro_join_lines(spec),
            "",
        ]
    )
    _write(result.output_dir / "models" / "generated" / f"{model_name}.sql", sql, result)
    result.files.append(seed_path)



def _write_csv_seed_file(spec: dict[str, Any], options: GenerateDbtOptions, result: DbtGenerationResult) -> Path:
    seed_file = csv_seed_file_path(spec, options.spec_path)
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
        positions = [
            int(field["source"]["pos"])
            for field in fields(spec)
            if isinstance(field.get("source"), dict) and isinstance(field["source"].get("pos"), int)
        ]
        column_count = max([*(pos + 1 for pos in positions), *(len(row) for row in rows)], default=0)
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
    flatten_aliases = _flatten_aliases(source)
    select_lines = []
    for field in fields(spec):
        column = _source_column_name(field)
        expression = (
            _source_macro_field_expression(field)
            or _table_field_source_expression(field, flatten_aliases)
        )
        select_lines.append(f"    {expression} as {_quote_identifier(column)}")
    select_lines.extend(_scd2_derived_source_helper_lines(spec))
    source_sql = _table_source_sql(source)
    type_guard_lines = _semistructured_source_type_guard_lines(spec)
    sql = "\n".join(
        [
            "{{",
            "  config(",
            *_model_config_lines(
                materialized="view",
                database=_target_relation_config(spec).database,
                schema=_staging_schema_config_expression(spec),
                alias=model_name,
                schema_is_expression=True,
            ),
            "  )",
            "}}",
            "",
            *type_guard_lines,
            *source_sql,
            "select",
            ",\n".join(select_lines),
            *_table_source_from_lines(spec),
            "",
        ]
    )
    _write(result.output_dir / "models" / "generated" / f"{model_name}.sql", sql, result)



def _source_model_name(target_id: str) -> str:
    return f"{target_id}__source"



def _csv_seed_name(spec: dict[str, Any]) -> str:
    seed = spec["source"].get("seed", {})
    if not isinstance(seed, dict):
        seed = {}
    return str(seed.get("name", f"{spec['target']['id']}__seed"))



def _seed_project_config(spec: dict[str, Any]) -> dict[str, Any] | None:
    source = spec.get("source", {})
    if not isinstance(source, dict) or source.get("format") != "csv" or csv_load_method(source) != "dbt_seed":
        return None

    seed = source.get("seed", {})
    if not isinstance(seed, dict):
        seed = {}
    seed_name = _csv_seed_name(spec)
    seed_schema = seed.get("schema")
    config: dict[str, Any] = {
        "+quote_columns": False,
        "+schema": (
            _physical_name(seed_schema)
            if seed_schema
            else "{{ var('tms_staging_schema', '" + _staging_schema(spec) + "') | upper }}"
        ),
        "+alias": _physical_name(seed_name),
        "+column_types": {
            _physical_name(column): "varchar"
            for column in _csv_physical_source_columns(spec)
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



def _table_source_sql(source: dict[str, Any]) -> list[str]:
    query = source.get("query")
    if isinstance(query, str) and query.strip():
        return ["with source_query as (", _indent_sql(query.strip()), ")", ""]
    return ["with source_query as (", f"    select * from {_table_source_relation(source)}", ")", ""]



def _table_source_from_lines(spec: dict[str, Any]) -> list[str]:
    source = spec["source"]
    lines = ["from source_query"]
    previous_aliases: set[str] = set()
    for entry in _flatten_entries(source):
        alias = _physical_name(entry["alias"])
        input_expression = _table_source_variant_expression(str(entry["column"]), previous_aliases)
        path = entry.get("path")
        if isinstance(path, str):
            input_expression = _snowflake_get_path_expression(input_expression, path)
        arguments = [f"input => {input_expression}"]
        if entry.get("outer") is True:
            arguments.append("outer => true")
        mode = str(entry.get("mode", "both")).upper()
        arguments.append(f"mode => {_sql_string(mode)}")
        lines.append(f", lateral flatten({', '.join(arguments)}) as {alias}")
        previous_aliases.add(case_key(str(entry["alias"])))
    lines.extend(_source_macro_join_lines(spec))
    return lines



def _source_macro_field_expression(field: dict[str, Any]) -> str | None:
    source = field.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("macro"), str):
        return None
    alias = _source_macro_alias(field)
    return f"{alias}.{_quote_identifier(str(field['id']))}"



def _source_macro_join_lines(spec: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for field in fields(spec):
        if not isinstance(field, dict):
            continue
        source = field.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("macro"), str):
            continue
        args = source.get("args", {})
        if not isinstance(args, dict):
            args = {}
        macro_args = dict(args)
        macro_args.setdefault("output_column", str(field["id"]))
        macro_args.setdefault("ref_alias", _source_macro_alias(field))
        lines.append(_source_macro_expression(str(source["macro"]), macro_args))
    return lines



def _source_macro_alias(field: dict[str, Any]) -> str:
    return _physical_name(f"lookup_{field['id']}")



def _table_field_source_expression(field: dict[str, Any], flatten_aliases: set[str]) -> str:
    source = field.get("source", {})
    if "fixed_value" in source:
        return _fixed_value_expression(source["fixed_value"])
    if "column" not in source and "default_value" in source:
        return _default_value_expression(source["default_value"])
    if "column" not in source and isinstance(source.get("default_from_field"), str):
        default_from_field = str(source["default_from_field"])
        return to_varchar_expression(_table_source_variant_expression(default_from_field, flatten_aliases))
    column = str(source.get("column", field["id"]))
    base_expression = _table_source_variant_expression(column, flatten_aliases)
    path = source.get("snowflake_path")
    expression: str
    if isinstance(path, str):
        expression = f"to_varchar({_snowflake_get_path_expression(base_expression, path)})"
    else:
        expression = base_expression
    return _apply_default_source_expression(expression, source, flatten_aliases)



def _table_source_variant_expression(column: str, flatten_aliases: set[str]) -> str:
    if case_key(column) in flatten_aliases:
        return f"{_physical_name(column)}.value"
    return f"source_query.{_quote_identifier(column)}"



def _snowflake_get_path_expression(expression: str, path: str) -> str:
    return f"get_path({expression}, {_sql_string(path)})"



def _fixed_value_expression(value: Any) -> str:
    return f"cast({_sql_scalar(value)} as string)"



def _default_value_expression(value: Any) -> str:
    return f"cast({_sql_scalar(value)} as string)"



def _source_macro_expression(macro_reference: str, args: Any) -> str:
    macro_name = _macro_object_name(macro_reference)
    if not isinstance(args, dict):
        args = {}
    formatted_args = ", ".join(
        f"{key}={_jinja_literal(value)}" for key, value in sorted(args.items())
    )
    return "{{ " + f"{macro_name}({formatted_args})" + " }}"



def _jinja_literal(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(str(value))



def _apply_default_value_expression(expression: str, source: dict[str, Any]) -> str:
    if "default_value" not in source:
        return expression
    return f"coalesce(nullif({expression}, ''), {_default_value_expression(source['default_value'])})"



def _apply_default_source_expression(
    expression: str,
    source: dict[str, Any],
    flatten_aliases: set[str],
) -> str:
    expressions = [f"nullif({_to_varchar_for_default(expression)}, '')"]
    default_from_field = source.get("default_from_field")
    if isinstance(default_from_field, str):
        default_expression = to_varchar_expression(
            _table_source_variant_expression(default_from_field, flatten_aliases)
        )
        expressions.append(f"nullif({default_expression}, '')")
    if "default_value" in source:
        expressions.append(_default_value_expression(source["default_value"]))
    if len(expressions) == 1:
        return expression
    return f"coalesce({', '.join(expressions)})"



def to_varchar_expression(expression: str) -> str:
    return f"to_varchar({expression})"



def _to_varchar_for_default(expression: str) -> str:
    if expression.startswith("to_varchar("):
        return expression
    return to_varchar_expression(expression)



def _defaulted_csv_source_value(source: dict[str, Any], value: str | None) -> str | None:
    if value in {None, ""} and "default_value" in source:
        default_value = source["default_value"]
        return None if default_value is None else str(default_value)
    return value



def _flatten_entries(source: dict[str, Any]) -> list[dict[str, Any]]:
    flatten = source.get("flatten")
    if isinstance(flatten, dict):
        return [flatten]
    if isinstance(flatten, list):
        return [entry for entry in flatten if isinstance(entry, dict)]
    return []



def _flatten_aliases(source: dict[str, Any]) -> set[str]:
    return {
        case_key(str(entry["alias"]))
        for entry in _flatten_entries(source)
        if isinstance(entry.get("alias"), str)
    }



def _semistructured_source_type_guard_lines(spec: dict[str, Any]) -> list[str]:
    source = spec["source"]
    if source.get("format") != "table" or source.get("query"):
        return []
    if _relation_has_templated_part(source):
        return []
    columns = sorted(_semistructured_source_columns_to_guard(spec))
    if not columns:
        return []

    database = source.get("database")
    information_schema = (
        f"{_physical_name(database)}.information_schema.columns"
        if database
        else "information_schema.columns"
    )
    column_list = ", ".join(_sql_string(column) for column in columns)
    return [
        "{% if execute %}",
        "{% set tms_semistructured_source_type_sql %}",
        "select column_name, data_type",
        f"from {information_schema}",
        f"where table_schema = {_sql_string(_physical_name(source['schema']))}",
        f"  and table_name = {_sql_string(_physical_name(source['table']))}",
        f"  and upper(column_name) in ({column_list})",
        "  and data_type not in ('VARIANT', 'OBJECT', 'ARRAY')",
        "{% endset %}",
        "{% set tms_semistructured_source_type_result = run_query(tms_semistructured_source_type_sql) %}",
        "{% if tms_semistructured_source_type_result is not none and (tms_semistructured_source_type_result.rows | length) > 0 %}",
        '  {{ exceptions.raise_compiler_error("field.source.snowflake_path requires Snowflake VARIANT, OBJECT, or ARRAY source columns") }}',
        "{% endif %}",
        "{% endif %}",
        "",
    ]



def _relation_has_templated_part(source: dict[str, Any]) -> bool:
    return any("{{" in str(source.get(part, "")) for part in ("database", "schema", "table"))



def _semistructured_source_columns_to_guard(spec: dict[str, Any]) -> set[str]:
    source = spec["source"]
    flatten_aliases = _flatten_aliases(source)
    guarded = {
        _physical_name(field["source"]["column"])
        for field in fields(spec)
        if isinstance(field.get("source"), dict)
        and isinstance(field["source"].get("column"), str)
        and isinstance(field["source"].get("snowflake_path"), str)
        and case_key(field["source"]["column"]) not in flatten_aliases
    }
    for entry in _flatten_entries(source):
        column = entry.get("column")
        if isinstance(column, str) and case_key(column) not in flatten_aliases:
            guarded.add(_physical_name(column))
    return guarded



def _source_output_columns(spec: dict[str, Any]) -> list[str]:
    target_fields = fields(spec)
    if spec["source"]["format"] == "csv":
        target_fields = sorted(
            target_fields,
            key=lambda field: field.get("source", {}).get("pos", 999999),
        )
    return [_source_column_name(field) for field in target_fields]



def _csv_physical_source_columns(spec: dict[str, Any]) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    target_fields = sorted(fields(spec), key=lambda field: field.get("source", {}).get("pos", 999999))
    for field in target_fields:
        source = field.get("source", {})
        if not isinstance(source, dict) or "fixed_value" in source:
            continue
        if isinstance(source.get("macro"), str):
            helper_column = _macro_helper_source_column_name(field)
            if helper_column is None:
                continue
            helper_key = case_key(helper_column)
            if helper_key not in seen:
                seen.add(helper_key)
                columns.append(helper_column)
            continue
        column = _source_column_name(field)
        column_key = case_key(column)
        if column_key not in seen:
            seen.add(column_key)
            columns.append(column)
    return columns



def _source_column_name(field: dict[str, Any]) -> str:
    source = field.get("source", {})
    if isinstance(source, dict) and isinstance(source.get("macro"), str):
        return _physical_name(field["id"])
    if isinstance(source, dict) and "fixed_value" in source:
        return _physical_name(field["id"])
    if isinstance(source, dict) and isinstance(source.get("snowflake_path"), str):
        return _physical_name(field["id"])
    column = source.get("column")
    if isinstance(column, str):
        return _physical_name(column)
    pos = source.get("pos")
    if isinstance(pos, int):
        return _position_column_name(pos)
    return _physical_name(field["id"])



def _position_column_name(pos: int) -> str:
    return f"COL_{pos}"



def _macro_helper_source_column_name(field: dict[str, Any]) -> str | None:
    source = field.get("source", {})
    if not isinstance(source, dict) or not isinstance(source.get("macro"), str):
        return None
    column = source.get("column")
    if isinstance(column, str):
        return _physical_name(column)
    pos = source.get("pos")
    if isinstance(pos, int):
        return _position_column_name(pos)
    return None
