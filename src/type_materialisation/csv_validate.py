import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP
from pathlib import Path
from typing import Any

from .custom_macros import MacroLoadError, PythonMacroResolver
from .errors import Diagnostic
from .inheritance import InheritanceError, resolve_spec
from .spec import SqlType, case_key, decimal_fits, fields, parse_sql_type, to_decimal


@dataclass
class CsvValidationResult:
    rows_checked: int = 0
    errors: list[Diagnostic] = field(default_factory=list)
    warnings: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_csv_file(
    spec_path: Path,
    csv_path: Path,
    *,
    macro_paths: list[Path] | None = None,
    spec: dict[str, Any] | None = None,
    spec_paths: list[Path] | None = None,
) -> CsvValidationResult:
    result = CsvValidationResult()
    if spec is None:
        try:
            spec = resolve_spec(spec_path, spec_paths=spec_paths).spec
        except InheritanceError as exc:
            result.errors.append(Diagnostic(str(exc), "inheritance"))
            return result
    source = spec.get("source", {})
    if not isinstance(source, dict) or source.get("format") != "csv":
        result.errors.append(Diagnostic("validate currently supports only CSV source specifications"))
        return result

    target_fields = fields(spec)
    try:
        dialect = _csv_dialect(source)
    except ValueError as exc:
        result.errors.append(Diagnostic(str(exc), "$.source.quoting"))
        return result
    header_enabled = source.get("header")
    macros = PythonMacroResolver(spec_path=spec_path, macro_paths=macro_paths)
    executable_macros = _preflight_custom_macros(target_fields, macros, result)
    if result.errors:
        return result

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, dialect=dialect)
        header: list[str] | None = None
        if header_enabled:
            try:
                header = next(reader)
            except StopIteration:
                result.errors.append(Diagnostic("CSV file is empty", str(csv_path)))
                return result
            _validate_header_mappings(target_fields, header, result)

        unique_values: dict[str, set[Any]] = defaultdict(set)
        for row_number, row in enumerate(reader, start=2 if header_enabled else 1):
            result.rows_checked += 1
            row_values: dict[str, Any] = {}
            for field_index, field in enumerate(target_fields):
                if not isinstance(field, dict):
                    continue
                field_id = field.get("id", f"field_{field_index}")
                location = f"row {row_number}, field `{field_id}`"
                try:
                    value = _extract_value(field, row, header)
                    value = _apply_transforms(value, field, macros, executable_macros)
                    value = _validate_type(value, field.get("data_type"), location)
                    _validate_nullable(value, field, location)
                    _validate_rules(value, field, macros, row_values, executable_macros)
                    if field.get("unique") is True and value is not None:
                        if value in unique_values[str(field_id)]:
                            raise ValueError("duplicates a value for a unique field")
                        unique_values[str(field_id)].add(value)
                    row_values[str(field_id)] = value
                except ValueError as exc:
                    result.errors.append(Diagnostic(str(exc), location))
    return result


def _preflight_custom_macros(
    target_fields: list[dict[str, Any]],
    macros: PythonMacroResolver,
    result: CsvValidationResult,
) -> set[str]:
    executable_macros: set[str] = set()
    seen: set[str] = set()
    for field_index, field in enumerate(target_fields):
        if not isinstance(field, dict):
            continue
        for rule_group_name in ("transforms", "validations"):
            rules = field.get(rule_group_name, [])
            if not isinstance(rules, list):
                continue
            for rule_index, rule in enumerate(rules):
                if not isinstance(rule, dict) or rule.get("type") != "custom":
                    continue
                macro_name = str(rule.get("macro"))
                if macro_name in seen:
                    continue
                seen.add(macro_name)
                location = f"$.target.fields[{field_index}].{rule_group_name}[{rule_index}].macro"
                try:
                    macros.require_sql_generation(macro_name)
                    if macros.has_python_callable(macro_name):
                        executable_macros.add(macro_name)
                    else:
                        result.warnings.append(
                            Diagnostic(
                                "custom macro has no Python execution callable; "
                                "tms validate will rely on generated SQL at dbt runtime",
                                location,
                            )
                        )
                except MacroLoadError as exc:
                    result.errors.append(Diagnostic(str(exc), location))
    return executable_macros


def _csv_dialect(source: dict[str, Any]) -> type[csv.Dialect]:
    quoting_name = source.get("quoting", "minimal")
    quoting_map = {
        "minimal": csv.QUOTE_MINIMAL,
        "all": csv.QUOTE_ALL,
    }
    if quoting_name not in quoting_map:
        raise ValueError("quoting must be one of: minimal, all")

    class SpecDialect(csv.Dialect):
        delimiter = source.get("delimiter", ",")
        quotechar = source.get("quotechar", '"')
        escapechar = None
        doublequote = True
        skipinitialspace = False
        lineterminator = source.get("lineterminator", "\r\n")
        quoting = quoting_map[quoting_name]
        strict = False

    return SpecDialect


def _validate_header_mappings(
    target_fields: list[dict[str, Any]], header: list[str], result: CsvValidationResult
) -> None:
    for field in target_fields:
        source = field.get("source", {}) if isinstance(field, dict) else {}
        if not isinstance(source, dict):
            continue
        pos = source.get("pos")
        column = source.get("column")
        if isinstance(pos, int) and pos >= len(header):
            result.errors.append(Diagnostic("field position is outside the CSV header", f"header pos {pos}"))
        if isinstance(column, str) and column not in header:
            result.errors.append(Diagnostic("field column is not present in the CSV header", f"header column `{column}`"))
        if isinstance(pos, int) and isinstance(column, str) and pos < len(header):
            if header[pos] != column:
                result.errors.append(
                    Diagnostic(
                        f"header at pos {pos} is `{header[pos]}`, expected `{column}`",
                        f"field `{field.get('id')}`",
                    )
                )


def _extract_value(field: dict[str, Any], row: list[str], header: list[str] | None) -> Any:
    source = field.get("source", {})
    if not isinstance(source, dict):
        raise ValueError("field source is missing")
    pos = source.get("pos")
    column = source.get("column")
    value: Any
    if isinstance(pos, int):
        if pos >= len(row):
            raise ValueError(f"CSV row does not contain position {pos}")
        value = _empty_to_none(row[pos])
    elif isinstance(column, str) and header is not None:
        try:
            index = header.index(column)
        except ValueError as exc:
            raise ValueError(f"CSV header does not contain column `{column}`") from exc
        if index >= len(row):
            raise ValueError(f"CSV row does not contain column `{column}`")
        value = _empty_to_none(row[index])
    else:
        raise ValueError("CSV field source must specify pos, or column with header")
    return _defaulted_source_value(source, value)


def _empty_to_none(value: str) -> str | None:
    return None if value == "" else value


def _defaulted_source_value(source: dict[str, Any], value: Any) -> Any:
    if value is None and "default_value" in source:
        default_value = source["default_value"]
        return None if default_value is None else str(default_value)
    return value


def _apply_transforms(
    value: Any,
    field: dict[str, Any],
    macros: PythonMacroResolver,
    executable_macros: set[str],
) -> Any:
    transforms = field.get("transforms", [])
    if value is None or not isinstance(transforms, list):
        return value
    current = value
    for transform in transforms:
        if not isinstance(transform, dict):
            continue
        transform_type = transform.get("type")
        if transform_type == "trim":
            side = transform.get("side", "both")
            if side == "left":
                current = str(current).lstrip()
            elif side == "right":
                current = str(current).rstrip()
            else:
                current = str(current).strip()
        elif transform_type == "parse_date":
            current = datetime.strptime(str(current), str(transform["format"])).date()
        elif transform_type == "parse_timestamp":
            current = _apply_time_if_missing(
                _timezone_aware_timestamp(
                    datetime.strptime(str(current), str(transform["format"])),
                    "timestamp transform result must include a timezone",
                    transform.get("timezone_if_missing"),
                ),
                transform,
            )
        elif transform_type == "round":
            scale = int(transform["scale"])
            mode = transform.get("mode", "half_up")
            rounding = {
                "half_up": ROUND_HALF_UP,
                "half_even": ROUND_HALF_EVEN,
                "down": ROUND_DOWN,
                "up": ROUND_UP,
            }[mode]
            current = to_decimal(current).quantize(Decimal(1).scaleb(-scale), rounding=rounding)
        elif transform_type == "custom":
            macro_name = str(transform["macro"])
            if macro_name not in executable_macros:
                continue
            try:
                current = macros.call(macro_name, value=current, field=field, rule=transform)
            except MacroLoadError as exc:
                raise ValueError(str(exc)) from exc
    return current


def _validate_type(value: Any, data_type: Any, location: str) -> Any:
    if value is None:
        return None
    sql_type = parse_sql_type(str(data_type))
    try:
        return _coerce_type(value, sql_type)
    except ValueError as exc:
        raise ValueError(f"does not match data_type `{data_type}`: {exc}") from exc


def _coerce_type(value: Any, sql_type: SqlType) -> Any:
    name = sql_type.name
    if name in {"char", "varchar", "character", "string", "text"}:
        text = str(value)
        if sql_type.args and len(text) > sql_type.args[0]:
            raise ValueError(f"length {len(text)} exceeds {sql_type.args[0]}")
        return text
    if name == "date":
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value)).date()
    if name in {"timestamp", "datetime", "timestamp_tz", "timestamptz"}:
        if isinstance(value, datetime):
            return _require_timezone(value, "must include a timezone")
        return _require_timezone(
            datetime.fromisoformat(str(value).replace("Z", "+00:00")),
            "must include a timezone",
        )
    if name in {"integer", "int", "bigint", "smallint"}:
        text = str(value)
        if re.fullmatch(r"[+-]?\d+", text) is None:
            raise ValueError("must be an integer")
        return int(text)
    if name in {"decimal", "numeric", "number"}:
        decimal_value = to_decimal(value)
        precision = sql_type.args[0]
        scale = sql_type.args[1] if len(sql_type.args) > 1 else None
        if not decimal_fits(decimal_value, precision, scale):
            raise ValueError("does not fit declared precision/scale")
        return decimal_value
    if name in {"float", "double"}:
        return float(value)
    if name in {"boolean", "bool"}:
        text = str(value).strip().lower()
        if text in {"true", "t", "1", "y", "yes"}:
            return True
        if text in {"false", "f", "0", "n", "no"}:
            return False
        raise ValueError("must be boolean")
    raise ValueError("unsupported type")


def _require_timezone(value: datetime, message: str) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(message)
    return value


def _timezone_aware_timestamp(
    value: datetime,
    message: str,
    timezone_if_missing: Any,
) -> datetime:
    if value.tzinfo is not None and value.tzinfo.utcoffset(value) is not None:
        return value
    if timezone_if_missing in {"Z", "UTC"}:
        return value.replace(tzinfo=timezone.utc)
    if timezone_if_missing == "local":
        return value.astimezone()
    raise ValueError(message)


def _apply_time_if_missing(value: datetime, transform: dict[str, Any]) -> datetime:
    if _python_datetime_format_has_time(str(transform["format"])):
        return value
    mode = transform.get("time_if_missing")
    if mode == "start_of_day":
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    if mode == "end_of_day":
        return value.replace(hour=23, minute=59, second=59, microsecond=999999)
    return value


def _python_datetime_format_has_time(python_format: str) -> bool:
    index = 0
    while index < len(python_format):
        if python_format[index] != "%":
            index += 1
            continue
        if index + 1 >= len(python_format):
            return False
        if python_format[index + 1] in {"H", "I", "M", "S", "f", "p"}:
            return True
        index += 2
    return False


def _validate_nullable(value: Any, field: dict[str, Any], location: str) -> None:
    if value is None and field.get("nullable") is False:
        raise ValueError("is null but field is not nullable")


def _validate_rules(
    value: Any,
    field: dict[str, Any],
    macros: PythonMacroResolver,
    row_values: dict[str, Any],
    executable_macros: set[str],
) -> None:
    if value is None:
        return
    validations = field.get("validations", [])
    if not isinstance(validations, list):
        return
    for rule in validations:
        if not isinstance(rule, dict):
            continue
        rule_type = rule.get("type")
        if rule_type == "min_length" and len(str(value)) < int(rule["value"]):
            raise ValueError(f"length is less than {rule['value']}")
        if rule_type == "max_length" and len(str(value)) > int(rule["value"]):
            raise ValueError(f"length is greater than {rule['value']}")
        if rule_type == "regex" and re.search(str(rule["pattern"]), str(value)) is None:
            raise ValueError(f"does not match regex `{rule['pattern']}`")
        if rule_type == "allowed_values" and not _is_allowed(value, rule.get("values", [])):
            raise ValueError("is not one of the allowed values")
        if rule_type == "min_value" and to_decimal(value) < to_decimal(rule["value"]):
            raise ValueError(f"is less than {rule['value']}")
        if rule_type == "max_value" and to_decimal(value) > to_decimal(rule["value"]):
            raise ValueError(f"is greater than {rule['value']}")
        if rule_type == "precision":
            precision = int(rule["precision"])
            scale = int(rule["scale"]) if "scale" in rule else None
            if not decimal_fits(to_decimal(value), precision, scale):
                raise ValueError("does not fit validation precision/scale")
        if rule_type == "custom":
            macro_name = str(rule["macro"])
            if macro_name not in executable_macros:
                continue
            try:
                result = macros.call(macro_name, value=value, row=row_values, field=field, rule=rule)
            except MacroLoadError as exc:
                raise ValueError(str(exc)) from exc
            if result is False:
                raise ValueError("custom validation failed")
            if isinstance(result, str) and result:
                raise ValueError(result)


def _business_key_field_keys(spec: dict[str, Any]) -> list[str]:
    control_data = spec.get("control_data", {})
    if not isinstance(control_data, dict):
        return []
    business_key = control_data.get("business_key", {})
    if not isinstance(business_key, dict):
        return []
    configured_fields = business_key.get("fields", [])
    if not isinstance(configured_fields, list):
        return []
    return [case_key(value) for value in configured_fields if isinstance(value, str)]


def _field_id_lookup(spec: dict[str, Any]) -> dict[str, str]:
    return {
        case_key(field["id"]): field["id"]
        for field in fields(spec)
        if isinstance(field, dict) and isinstance(field.get("id"), str)
    }


def _is_allowed(value: Any, allowed_values: list[Any]) -> bool:
    return value in allowed_values or str(value) in {str(item) for item in allowed_values}
