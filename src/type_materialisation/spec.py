import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .errors import Diagnostic

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

SUPPORTED_TYPE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)(?:\(([^)]*)\))?\s*$")
JINJA_EXPR_RE = re.compile(r"{{.*?}}")
SUPPORTED_JINJA_EXPR_RE = re.compile(
    r"""^\s*{{\s*(var|env_var)\(\s*(['"])[^'"]+\2\s*(,\s*(['"])[^'"]*\4\s*)?\)\s*}}\s*$"""
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
    diagnostics.extend(_validate_csv_seed_source(spec))
    diagnostics.extend(_validate_table_query(spec))
    diagnostics.extend(_validate_scd(spec, abstract=abstract))
    return diagnostics


def _validate_target_fields(spec: dict[str, Any]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    seen: dict[str, str] = {}
    seen_source_columns: dict[str, str] = {}
    for index, field in enumerate(fields(spec)):
        if not isinstance(field, dict):
            continue
        field_id = field.get("id")
        if not isinstance(field_id, str):
            continue
        key = case_key(field_id)
        location = f"$.target.fields[{index}].id"
        if key in RESERVED_GENERATED_FIELDS:
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
        if source.get("header") is True and not isinstance(field_source.get("column"), str):
            diagnostics.append(
                Diagnostic(
                    "dbt_seed CSV sources with a header require field.source.column",
                    f"$.target.fields[{index}].source.column",
                )
            )
        if source.get("header") is False and not isinstance(field_source.get("pos"), int):
            diagnostics.append(
                Diagnostic(
                    "dbt_seed CSV sources without a header require field.source.pos",
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
    diagnostics: list[Diagnostic] = []
    control_data = spec.get("control_data")
    if not isinstance(control_data, dict):
        return diagnostics
    change_type = control_data.get("change_type")
    scd = control_data.get("scd")
    field_ids = {case_key(field.get("id")) for field in fields(spec) if isinstance(field.get("id"), str)}

    if change_type == "scd2":
        if not isinstance(scd, dict):
            diagnostics.append(Diagnostic("`scd` is required when `change_type` is scd2", "$.control_data"))
            return diagnostics
        business_key = scd.get("business_key")
        if not isinstance(business_key, list) or not business_key:
            diagnostics.append(Diagnostic("`business_key` must contain at least one field id", "$.control_data.scd"))
        elif field_ids:
            for index, key in enumerate(business_key):
                if isinstance(key, str) and case_key(key) not in field_ids:
                    diagnostics.append(
                        Diagnostic("business key field does not exist in target.fields", f"$.control_data.scd.business_key[{index}]")
                    )

    if not isinstance(scd, dict):
        return diagnostics

    delete_detection = scd.get("delete_detection")
    if isinstance(delete_detection, dict):
        field = delete_detection.get("field")
        if isinstance(field, str) and field_ids and case_key(field) not in field_ids:
            diagnostics.append(
                Diagnostic("referenced field does not exist in target.fields", "$.control_data.scd.delete_detection.field")
            )

    if change_type != "scd2":
        return diagnostics

    if scd.get("valid_from_to_mode", "continuous") == "sparse":
        diagnostics.append(
            Diagnostic("valid_from_to_mode `sparse` is not implemented yet", "$.control_data.scd.valid_from_to_mode")
        )

    valid_from_datetime = scd.get("valid_from_datetime")
    valid_from_selection = "load_datetime"
    if isinstance(valid_from_datetime, dict):
        valid_from_selection = str(valid_from_datetime.get("valid_from_datetime_selection", valid_from_selection))
    if (
        isinstance(delete_detection, dict)
        and delete_detection.get("mode") == "missing_from_source"
        and valid_from_selection == "field"
    ):
        diagnostics.append(
            Diagnostic(
                "delete_detection.mode `missing_from_source` is invalid when valid_from_datetime_selection is field",
                "$.control_data.scd.delete_detection.mode",
            )
        )

    for section_name in ("valid_from_datetime", "valid_to_datetime"):
        section = scd.get(section_name)
        if not isinstance(section, dict):
            continue
        field = section.get("field")
        if isinstance(field, str) and field_ids and case_key(field) not in field_ids:
            diagnostics.append(
                Diagnostic("referenced field does not exist in target.fields", f"$.control_data.scd.{section_name}.field")
            )

    hash_config = scd.get("business_data_hash")
    if isinstance(hash_config, dict):
        mode = hash_config.get("mode")
        hash_fields = hash_config.get("fields")
        if mode == "include" and not hash_fields:
            diagnostics.append(
                Diagnostic("include mode requires at least one field", "$.control_data.scd.business_data_hash.fields")
            )
        if isinstance(hash_fields, list) and field_ids:
            for index, value in enumerate(hash_fields):
                if isinstance(value, str) and case_key(value) not in field_ids:
                    diagnostics.append(
                        Diagnostic("hash field does not exist in target.fields", f"$.control_data.scd.business_data_hash.fields[{index}]")
                    )
    return diagnostics


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
