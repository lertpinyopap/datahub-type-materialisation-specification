import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .errors import Diagnostic

BUSINESS_KEY_DATA_TYPE = "varchar"
SURROGATE_KEY_DATA_TYPE = "varchar(36)"

GENERATED_METADATA_FIELD_TYPES = {
    "is_current_flag": "varchar(1)",
    "is_deleted_flag": "varchar(1)",
    "valid_from_datetime": "timestamp_tz",
    "valid_to_datetime": "timestamp_tz",
    "business_data_hash": "varchar(64)",
    "audit_created_datetime": "timestamp_tz",
    "audit_last_changed_datetime": "timestamp_tz",
    "audit_data_process_key": "varchar(64)",
}
RESERVED_GENERATED_FIELDS = set(GENERATED_METADATA_FIELD_TYPES)
SCD2_MANUAL_FIELD_TYPES = {
    "valid_from_datetime": "timestamp",
    "valid_to_datetime": "timestamp",
    "is_current_flag": "varchar(1)",
    "is_deleted_flag": "varchar(1)",
}

SUPPORTED_TYPE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)(?:\(([^)]*)\))?\s*$")
JINJA_EXPR_RE = re.compile(r"{{.*?}}")
SUPPORTED_JINJA_EXPR_RE = re.compile(
    r"""^\s*{{\s*(var|env_var|tms_var)\(\s*(['"])[^'"]+\2\s*(,\s*(['"])[^'"]*\4\s*)?\)\s*}}\s*$"""
)


@dataclass(frozen=True)
class SqlType:
    name: str
    args: tuple[int, ...] = ()


def case_key(value: str) -> str:
    return value.casefold()


def fields(spec: dict[str, Any]) -> list[dict[str, Any]]:
    target = spec.get("target")
    if not isinstance(target, dict):
        return []
    value = target.get("fields", [])
    return value if isinstance(value, list) else []


def _change_type(spec: dict[str, Any]) -> str | None:
    control_data = spec.get("control_data", {})
    if not isinstance(control_data, dict):
        return None
    value = control_data.get("change_type")
    return str(value) if value is not None else None


def parse_sql_type(value: str) -> SqlType:
    match = SUPPORTED_TYPE_RE.match(value)
    if not match:
        raise ValueError("must be a SQL-like type declaration")

    name = match.group(1).lower()
    raw_args = match.group(2)
    args: tuple[int, ...] = ()
    if raw_args is not None:
        parts = [part.strip() for part in raw_args.split(",")]
        if not all(part.isdigit() for part in parts):
            raise ValueError("type arguments must be non-negative integers")
        args = tuple(int(part) for part in parts)

    no_arg_types = {
        "string",
        "text",
        "date",
        "timestamp",
        "timestamp_tz",
        "timestamptz",
        "datetime",
        "boolean",
        "bool",
        "integer",
        "int",
        "bigint",
        "smallint",
        "float",
        "double",
        "char",
        "varchar",
        "character",
    }
    one_arg_types = {"char", "varchar", "character", "string"}
    two_arg_types = {"decimal", "numeric", "number"}

    if name in no_arg_types and not args:
        return SqlType(name)
    if name in one_arg_types and len(args) == 1 and args[0] > 0:
        return SqlType(name, args)
    if name in two_arg_types and len(args) in {1, 2}:
        if args[0] < 1:
            raise ValueError("precision must be at least 1")
        if len(args) == 2 and args[1] < 0:
            raise ValueError("scale must be at least 0")
        if len(args) == 2 and args[1] > args[0]:
            raise ValueError("scale must be less than or equal to precision")
        return SqlType(name, args)

    raise ValueError(f"unsupported data type `{value}`")


def validate_semantics(spec: dict[str, Any], *, abstract: bool) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    diagnostics.extend(_validate_jinja(spec))
    diagnostics.extend(_validate_target_fields(spec))
    diagnostics.extend(_validate_fixed_value_sources(spec))
    diagnostics.extend(_validate_snowflake_table_extraction(spec))
    diagnostics.extend(_validate_csv_seed_source(spec))
    diagnostics.extend(_validate_table_query(spec))
    diagnostics.extend(_validate_surrogate_key(spec))
    diagnostics.extend(_validate_business_key(spec, abstract=abstract))
    diagnostics.extend(_validate_business_data_hash(spec))
    diagnostics.extend(_validate_scd(spec, abstract=abstract))
    return diagnostics


def _validate_target_fields(spec: dict[str, Any]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    seen: dict[str, str] = {}
    seen_source_columns: dict[str, str] = {}
    reserved_generated_fields = _reserved_generated_fields(spec)
    for index, field in enumerate(fields(spec)):
        if not isinstance(field, dict):
            continue
        field_id = field.get("id")
        if not isinstance(field_id, str):
            continue
        key = case_key(field_id)
        location = f"$.target.fields[{index}].id"
        if key in reserved_generated_fields:
            diagnostics.append(Diagnostic("uses a reserved generated metadata field id", location))
        if key in seen:
            diagnostics.append(
                Diagnostic(f"duplicates field id `{seen[key]}` case-insensitively", location)
            )
        seen[key] = field_id

        source = field.get("source", {})
        if not isinstance(source, dict):
            continue
        column = source.get("column")
        if not isinstance(column, str):
            continue
        if "snowflake_path" in source:
            continue
        column_key = case_key(column)
        column_location = f"$.target.fields[{index}].source.column"
        if column_key in seen_source_columns:
            diagnostics.append(
                Diagnostic(
                    f"duplicates source column `{seen_source_columns[column_key]}` case-insensitively",
                    column_location,
                )
            )
        seen_source_columns[column_key] = column
    return diagnostics


def _validate_snowflake_table_extraction(spec: dict[str, Any]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    source = spec.get("source")
    if not isinstance(source, dict):
        return diagnostics
    source_format = source.get("format")
    flatten_aliases = _flatten_aliases(source)

    if "flatten" in source and source_format != "table":
        diagnostics.append(Diagnostic("source.flatten is only valid for table sources", "$.source.flatten"))

    diagnostics.extend(_validate_flatten_entries(source))

    ordinary_source_columns = {
        case_key(field_source["column"])
        for field in fields(spec)
        if isinstance(field, dict)
        for field_source in [field.get("source")]
        if isinstance(field_source, dict)
        and isinstance(field_source.get("column"), str)
        and "snowflake_path" not in field_source
    }
    for alias, location in flatten_aliases.items():
        if alias in ordinary_source_columns:
            diagnostics.append(
                Diagnostic(
                    "source.flatten.alias duplicates a field.source.column that is used without snowflake_path",
                    location,
                )
            )

    for index, field in enumerate(fields(spec)):
        if not isinstance(field, dict):
            continue
        field_source = field.get("source")
        if not isinstance(field_source, dict):
            continue
        has_path = "snowflake_path" in field_source
        column = field_source.get("column")
        if has_path and source_format != "table":
            diagnostics.append(
                Diagnostic(
                    "field.source.snowflake_path is only valid for table sources",
                    f"$.target.fields[{index}].source.snowflake_path",
                )
            )
        if has_path and not isinstance(column, str):
            diagnostics.append(
                Diagnostic(
                    "field.source.snowflake_path requires field.source.column",
                    f"$.target.fields[{index}].source.column",
                )
            )
        if (
            not has_path
            and isinstance(column, str)
            and case_key(column) in flatten_aliases
        ):
            diagnostics.append(
                Diagnostic(
                    "field.source.column references a flatten alias and requires snowflake_path",
                    f"$.target.fields[{index}].source.snowflake_path",
                )
            )
    return diagnostics


def _validate_fixed_value_sources(spec: dict[str, Any]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for index, field in enumerate(fields(spec)):
        if not isinstance(field, dict):
            continue
        field_source = field.get("source")
        if not isinstance(field_source, dict) or "fixed_value" not in field_source:
            continue
        for selector in ("pos", "column", "snowflake_path"):
            if selector in field_source:
                diagnostics.append(
                    Diagnostic(
                        f"field.source.fixed_value cannot be combined with field.source.{selector}",
                        f"$.target.fields[{index}].source.fixed_value",
                    )
                )
    return diagnostics


def _validate_flatten_entries(source: dict[str, Any]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    seen_aliases: dict[str, str] = {}
    for index, entry in enumerate(_flatten_entries(source)):
        location = _flatten_entry_location(source, index)
        alias = entry.get("alias")
        if isinstance(alias, str):
            key = case_key(alias)
            alias_location = f"{location}.alias"
            if key in seen_aliases:
                diagnostics.append(
                    Diagnostic(
                        f"duplicates source.flatten.alias `{seen_aliases[key]}` case-insensitively",
                        alias_location,
                    )
                )
            seen_aliases[key] = alias
    return diagnostics


def _flatten_entries(source: dict[str, Any]) -> list[dict[str, Any]]:
    flatten = source.get("flatten")
    if isinstance(flatten, dict):
        return [flatten]
    if isinstance(flatten, list):
        return [entry for entry in flatten if isinstance(entry, dict)]
    return []


def _flatten_aliases(source: dict[str, Any]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for index, entry in enumerate(_flatten_entries(source)):
        alias = entry.get("alias")
        if isinstance(alias, str):
            aliases[case_key(alias)] = f"{_flatten_entry_location(source, index)}.alias"
    return aliases


def _flatten_entry_location(source: dict[str, Any], index: int) -> str:
    return "$.source.flatten" if isinstance(source.get("flatten"), dict) else f"$.source.flatten[{index}]"


def _reserved_generated_fields(spec: dict[str, Any]) -> set[str]:
    reserved = set(RESERVED_GENERATED_FIELDS)
    if _business_key_enabled(spec):
        reserved.add(_business_key_generated_name(spec))
    if _surrogate_key_enabled(spec):
        reserved.add(_surrogate_key_name(spec))
    if _change_type(spec) == "scd2_manual":
        reserved.difference_update(SCD2_MANUAL_FIELD_TYPES)
    return reserved


def _walk(value: Any, path: str = "$") -> Iterable[tuple[str, Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


def _validate_jinja(spec: dict[str, Any]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for path, value in _walk(spec):
        if not isinstance(value, str) or "{{" not in value:
            continue
        for expression in JINJA_EXPR_RE.findall(value):
            if not SUPPORTED_JINJA_EXPR_RE.match(expression):
                diagnostics.append(Diagnostic(f"unsupported Jinja expression `{expression}`", path))
    return diagnostics


def _validate_csv_seed_source(spec: dict[str, Any]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    source = spec.get("source")
    if not isinstance(source, dict):
        return diagnostics
    if source.get("format") != "csv" or source.get("load_method", "stage") != "dbt_seed":
        return diagnostics

    for index, field in enumerate(fields(spec)):
        if not isinstance(field, dict):
            continue
        field_source = field.get("source")
        if not isinstance(field_source, dict):
            diagnostics.append(
                Diagnostic(
                    "dbt_seed CSV sources require field.source",
                    f"$.target.fields[{index}].source",
                )
            )
            continue
        if "fixed_value" in field_source:
            continue
        if source.get("header") is True and not isinstance(field_source.get("column"), str):
            diagnostics.append(
                Diagnostic(
                    "dbt_seed CSV sources with a header require field.source.column or field.source.fixed_value",
                    f"$.target.fields[{index}].source.column",
                )
            )
        if source.get("header") is False and not isinstance(field_source.get("pos"), int):
            diagnostics.append(
                Diagnostic(
                    "dbt_seed CSV sources without a header require field.source.pos or field.source.fixed_value",
                    f"$.target.fields[{index}].source.pos",
                )
            )
    return diagnostics


def _validate_table_query(spec: dict[str, Any]) -> list[Diagnostic]:
    source = spec.get("source")
    if not isinstance(source, dict) or source.get("format") != "table":
        return []
    query = source.get("query")
    if not isinstance(query, str):
        return []
    stripped = query.strip()
    diagnostics: list[Diagnostic] = []
    if ";" in stripped:
        diagnostics.append(Diagnostic("table source query must be a single SQL query without `;`", "$.source.query"))
    if not re.match(r"(?is)^(select|with)\b", stripped):
        diagnostics.append(Diagnostic("table source query must start with SELECT or WITH", "$.source.query"))
    return diagnostics


def _validate_data_type_value(value: Any, location: str) -> list[Diagnostic]:
    if not isinstance(value, str):
        return []
    try:
        parse_sql_type(value)
    except ValueError as exc:
        return [Diagnostic(str(exc), location)]
    return []


def _validate_regex_value(value: Any, location: str) -> list[Diagnostic]:
    if not isinstance(value, str):
        return []
    try:
        re.compile(value)
    except re.error as exc:
        return [Diagnostic(f"invalid regular expression: {exc}", location)]
    return []


def _validate_scd(spec: dict[str, Any], *, abstract: bool) -> list[Diagnostic]:
    del abstract
    diagnostics: list[Diagnostic] = []
    control_data = spec.get("control_data")
    if not isinstance(control_data, dict):
        return diagnostics
    change_type = control_data.get("change_type")
    scd = control_data.get("scd")
    target_fields = fields(spec)
    field_ids = {
        case_key(field["id"])
        for field in target_fields
        if isinstance(field, dict) and isinstance(field.get("id"), str)
    }

    if change_type == "scd2_auto":
        if not isinstance(scd, dict) or "insert_time" not in scd:
            diagnostics.append(Diagnostic("`scd.insert_time` is required when `change_type` is scd2_auto", "$.control_data.scd.insert_time"))

    if change_type == "scd2_manual":
        if isinstance(scd, dict):
            invalid_keys = set(scd) - {"update_mode", "update_key"}
            if invalid_keys:
                diagnostics.append(
                    Diagnostic(
                        "`scd2_manual` supports only `scd.update_mode` and `scd.update_key`",
                        "$.control_data.scd",
                    )
                )
            if "update_key" in scd and scd.get("update_mode", "append_only") != "upsert":
                diagnostics.append(
                    Diagnostic(
                        "`scd.update_key` is only valid when `scd.update_mode` is upsert",
                        "$.control_data.scd.update_key",
                    )
                )
            update_key = scd.get("update_key")
            if isinstance(update_key, dict):
                update_key_fields = update_key.get("fields", [])
                if isinstance(update_key_fields, list):
                    for index, field_id in enumerate(update_key_fields):
                        if isinstance(field_id, str) and field_ids and case_key(field_id) not in field_ids:
                            diagnostics.append(
                                Diagnostic(
                                    "update key field does not exist in target.fields",
                                    f"$.control_data.scd.update_key.fields[{index}]",
                                )
                            )
        diagnostics.extend(_validate_scd2_manual_fields(spec))

    if not isinstance(scd, dict):
        return diagnostics

    if change_type == "scd1" and "insert_time" in scd:
        diagnostics.append(Diagnostic("`insert_time` is only valid when `change_type` is scd2_auto", "$.control_data.scd.insert_time"))
    if change_type == "scd1" and "scd2_auto_from_sot" in scd:
        diagnostics.append(
            Diagnostic(
                "`scd2_auto_from_sot` is only valid when `change_type` is scd2_auto",
                "$.control_data.scd.scd2_auto_from_sot",
            )
        )
    if change_type == "scd1" and "scd2_validation" in scd:
        diagnostics.append(
            Diagnostic(
                "`scd2_validation` is only valid when `change_type` is scd2_auto",
                "$.control_data.scd.scd2_validation",
            )
        )
    if change_type in {"scd1", "scd2_auto"} and "update_mode" in scd:
        diagnostics.append(
            Diagnostic(
                "`update_mode` is only valid when `change_type` is scd2_manual",
                "$.control_data.scd.update_mode",
            )
        )
    if change_type in {"scd1", "scd2_auto"} and "update_key" in scd:
        diagnostics.append(
            Diagnostic(
                "`update_key` is only valid when `change_type` is scd2_manual",
                "$.control_data.scd.update_key",
            )
        )
    delete_detection = scd.get("delete_detection")
    if isinstance(delete_detection, dict):
        if change_type == "scd2_auto":
            diagnostics.append(
                Diagnostic(
                    "`delete_detection.mode = field` is only valid when `change_type` is scd1",
                    "$.control_data.scd.delete_detection.mode",
                )
            )
        field = delete_detection.get("field")
        if isinstance(field, str) and field_ids and case_key(field) not in field_ids:
            diagnostics.append(
                Diagnostic("referenced field does not exist in target.fields", "$.control_data.scd.delete_detection.field")
            )
    return diagnostics


def _validate_surrogate_key(spec: dict[str, Any]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if not _surrogate_key_enabled(spec):
        return diagnostics
    generated_name = _surrogate_key_name(spec)
    target_field_ids = {
        case_key(field["id"])
        for field in fields(spec)
        if isinstance(field, dict) and isinstance(field.get("id"), str)
    }
    reserved = set(RESERVED_GENERATED_FIELDS)
    business_key_name = _business_key_generated_name(spec)
    if generated_name in target_field_ids or generated_name in reserved or generated_name == business_key_name:
        diagnostics.append(
            Diagnostic(
                "surrogate key name collides with a target field or generated metadata field",
                "$.control_data.skip_surrogate_key",
            )
        )
    return diagnostics


def _validate_business_key(spec: dict[str, Any], *, abstract: bool) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    control_data = spec.get("control_data")
    if not isinstance(control_data, dict):
        return diagnostics
    business_key = control_data.get("business_key")
    business_key_required = _business_key_required(spec)
    if business_key is None:
        if business_key_required and not abstract:
            diagnostics.append(Diagnostic("`business_key` is required", "$.control_data.business_key"))
        return diagnostics
    if not isinstance(business_key, dict):
        return diagnostics
    configured_fields = business_key.get("fields")
    target_field_ids = {
        case_key(field["id"])
        for field in fields(spec)
        if isinstance(field, dict) and isinstance(field.get("id"), str)
    }
    generated_name = _business_key_generated_name(spec)
    reserved = set(RESERVED_GENERATED_FIELDS)
    if _business_key_enabled(spec) and (generated_name in target_field_ids or generated_name in reserved):
        diagnostics.append(
            Diagnostic(
                "business key name collides with a target field or generated metadata field",
                "$.control_data.business_key",
            )
        )
    if not isinstance(configured_fields, list):
        return diagnostics
    if not target_field_ids:
        return diagnostics
    surrogate_key_name = _surrogate_key_name(spec) if _surrogate_key_enabled(spec) else None
    for index, field_id in enumerate(configured_fields):
        if not isinstance(field_id, str):
            continue
        field_key = case_key(field_id)
        location = f"$.control_data.business_key.fields[{index}]"
        if surrogate_key_name is not None and field_key == surrogate_key_name:
            diagnostics.append(
                Diagnostic(
                    "business key field must not reference generated surrogate key",
                    location,
                )
            )
            continue
        if field_key not in target_field_ids:
            diagnostics.append(
                Diagnostic(
                    "business key field does not exist in target.fields",
                    location,
                )
            )
    return diagnostics


def _validate_business_data_hash(spec: dict[str, Any]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    control_data = spec.get("control_data")
    if not isinstance(control_data, dict):
        return diagnostics
    config = control_data.get("business_data_hash")
    if not isinstance(config, dict):
        return diagnostics
    if config.get("skip_business_data_hash") is True:
        return diagnostics
    mode = config.get("business_data_hash_mode", "exclude")
    configured_fields = config.get("fields", [])
    if mode == "include" and not configured_fields:
        diagnostics.append(
            Diagnostic("include mode requires at least one field", "$.control_data.business_data_hash.fields")
        )
    if not isinstance(configured_fields, list):
        return diagnostics
    target_field_ids = {
        case_key(field["id"])
        for field in fields(spec)
        if isinstance(field, dict) and isinstance(field.get("id"), str)
    }
    if not target_field_ids:
        return diagnostics
    ineligible_fields = _business_data_hash_ineligible_field_ids(spec)
    for index, field_id in enumerate(configured_fields):
        if not isinstance(field_id, str):
            continue
        field_key = case_key(field_id)
        location = f"$.control_data.business_data_hash.fields[{index}]"
        if mode == "include" and field_key in ineligible_fields:
            diagnostics.append(
                Diagnostic(
                    "business data hash include field must not reference generated keys or SCD2 metadata fields",
                    location,
                )
            )
            continue
        if field_key not in target_field_ids:
            diagnostics.append(Diagnostic("business data hash field does not exist in target.fields", location))
    return diagnostics


def _business_key_generated_name(spec: dict[str, Any]) -> str:
    return _default_business_key_name(spec)


def _default_business_key_name(spec: dict[str, Any]) -> str:
    target = spec.get("target")
    if not isinstance(target, dict) or not isinstance(target.get("id"), str):
        return "business_key"
    return case_key(f"{_target_key_base(target['id'])}_business_key")


def _business_key_enabled(spec: dict[str, Any]) -> bool:
    control_data = spec.get("control_data")
    if not isinstance(control_data, dict):
        return True
    return control_data.get("skip_business_key") is not True


def _business_key_required(spec: dict[str, Any]) -> bool:
    return _business_key_enabled(spec) or _change_type(spec) == "scd2_auto"


def _surrogate_key_enabled(spec: dict[str, Any]) -> bool:
    control_data = spec.get("control_data")
    if not isinstance(control_data, dict):
        return True
    return control_data.get("skip_surrogate_key") is not True


def _surrogate_key_name(spec: dict[str, Any]) -> str:
    target = spec.get("target")
    if not isinstance(target, dict) or not isinstance(target.get("id"), str):
        return "key"
    return case_key(f"{_target_key_base(target['id'])}_key")


def _target_key_base(target_id: str) -> str:
    return target_id.rsplit("__", 1)[-1]


def _business_data_hash_ineligible_field_ids(spec: dict[str, Any]) -> set[str]:
    excluded_fields = set(RESERVED_GENERATED_FIELDS)
    if _business_key_enabled(spec):
        excluded_fields.add(_business_key_generated_name(spec))
    if _surrogate_key_enabled(spec):
        excluded_fields.add(_surrogate_key_name(spec))
    if _change_type(spec) == "scd2_manual":
        excluded_fields.update(SCD2_MANUAL_FIELD_TYPES)
        excluded_fields.update(_scd2_manual_update_key_field_ids(spec))
    return excluded_fields


def _scd2_manual_update_key_field_ids(spec: dict[str, Any]) -> set[str]:
    control_data = spec.get("control_data")
    if not isinstance(control_data, dict):
        return set()
    scd = control_data.get("scd")
    if not isinstance(scd, dict) or scd.get("update_mode", "append_only") != "upsert":
        return set()
    update_key = scd.get("update_key")
    if not isinstance(update_key, dict):
        return {"valid_from_datetime"}
    configured_fields = update_key.get("fields")
    if not isinstance(configured_fields, list) or not configured_fields:
        return {"valid_from_datetime"}
    return {case_key(field_id) for field_id in configured_fields if isinstance(field_id, str)}


def _validate_scd2_manual_fields(spec: dict[str, Any]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    fields_by_id = {
        case_key(field["id"]): (index, field)
        for index, field in enumerate(fields(spec))
        if isinstance(field, dict) and isinstance(field.get("id"), str)
    }
    for required_id in SCD2_MANUAL_FIELD_TYPES:
        if required_id not in fields_by_id:
            diagnostics.append(
                Diagnostic(
                    f"`scd2_manual` requires target field `{required_id}`",
                    "$.target.fields",
                )
            )
            continue
        index, field = fields_by_id[required_id]
        source = field.get("source")
        required_column = required_id.upper()
        if not isinstance(source, dict) or source.get("column") != required_column:
            diagnostics.append(
                Diagnostic(
                    f"`{required_id}` must map from source column `{required_column}` for `scd2_manual`",
                    f"$.target.fields[{index}].source.column",
                )
            )
        data_type = field.get("data_type")
        if not isinstance(data_type, str):
            continue
        if required_id in {"valid_from_datetime", "valid_to_datetime"}:
            if not _is_timestamp_type(data_type):
                diagnostics.append(
                    Diagnostic(
                        f"`{required_id}` must use a timestamp data type for `scd2_manual`",
                        f"$.target.fields[{index}].data_type",
                    )
                )
        elif not _is_varchar_1_type(data_type):
            diagnostics.append(
                Diagnostic(
                    f"`{required_id}` must use data_type `varchar(1)` for `scd2_manual`",
                    f"$.target.fields[{index}].data_type",
                )
            )
    return diagnostics


def _is_timestamp_type(value: str) -> bool:
    try:
        parsed = parse_sql_type(value)
    except ValueError:
        return False
    return parsed.name in {"timestamp", "timestamp_tz", "timestamptz", "datetime"}


def _is_varchar_1_type(value: str) -> bool:
    try:
        parsed = parse_sql_type(value)
    except ValueError:
        return False
    return parsed.name in {"char", "varchar", "character", "string"} and parsed.args == (1,)


def validate_field_details(spec: dict[str, Any]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for field_index, field in enumerate(fields(spec)):
        if not isinstance(field, dict):
            continue
        diagnostics.extend(
            _validate_data_type_value(field.get("data_type"), f"$.target.fields[{field_index}].data_type")
        )
        validations = field.get("validations", [])
        if isinstance(validations, list):
            for rule_index, rule in enumerate(validations):
                if isinstance(rule, dict) and rule.get("type") == "regex":
                    diagnostics.extend(
                        _validate_regex_value(
                            rule.get("pattern"),
                            f"$.target.fields[{field_index}].validations[{rule_index}].pattern",
                        )
                    )
    return diagnostics


def validate_parse_semantics(spec: dict[str, Any], *, abstract: bool) -> list[Diagnostic]:
    diagnostics = validate_semantics(spec, abstract=abstract)
    diagnostics.extend(validate_field_details(spec))
    return diagnostics


def decimal_fits(value: Decimal, precision: int, scale: int | None) -> bool:
    sign, digits, exponent = value.as_tuple()
    del sign
    if exponent >= 0:
        integer_digits = len(digits) + exponent
        fraction_digits = 0
    else:
        integer_digits = max(len(digits) + exponent, 0)
        fraction_digits = -exponent
    actual_precision = integer_digits + fraction_digits
    return actual_precision <= precision and (scale is None or fraction_digits <= scale)


def to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("must be numeric") from exc
