from __future__ import annotations

import csv
import json
import os
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import snowflake.connector
import yaml

from type_materialisation.spec import (
    BUSINESS_KEY_DATA_TYPE,
    GENERATED_METADATA_FIELD_TYPES,
    SURROGATE_KEY_DATA_TYPE,
)

DEFAULT_SNOWFLAKE_CONNECTION = "tms_int"


class IntegrationConfigError(RuntimeError):
    """Raised when live database configuration is incomplete."""


def live_database_enabled() -> bool:
    return os.environ.get("TMS_RUN_INTEGRATION") == "1"


@contextmanager
def snowflake_connection() -> Iterator[Any]:
    connect_kwargs = load_snowflake_connection_config()
    connection = snowflake.connector.connect(**connect_kwargs)
    try:
        yield connection
    finally:
        connection.close()


def load_snowflake_connection_config(
    *,
    config_path: Path | None = None,
    connection_name: str | None = None,
) -> dict[str, Any]:
    path = config_path or Path.home() / ".snowflake" / "config.toml"
    if not path.exists():
        raise IntegrationConfigError(
            f"Snowflake config file was not found at {path}. "
            "Create it with a [connections.tms_int] profile, or set "
            "TMS_SNOWFLAKE_CONNECTION to a profile that exists in that file."
        )
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    connections = config.get("connections")
    if not isinstance(connections, dict) or not connections:
        raise IntegrationConfigError(f"Snowflake config has no [connections.<name>] entries: {path}")
    selected_name = connection_name or os.environ.get("TMS_SNOWFLAKE_CONNECTION") or _default_connection_name(config, connections)
    raw_connection = connections.get(selected_name)
    if not isinstance(raw_connection, dict):
        available = ", ".join(sorted(str(name) for name in connections))
        raise IntegrationConfigError(
            f"Snowflake connection profile `{selected_name}` was not found in {path}. "
            f"Add [connections.{selected_name}] or set TMS_SNOWFLAKE_CONNECTION to one of: {available}"
        )
    connect_kwargs = _normalise_snowflake_connection(raw_connection)
    missing = [name for name in ("account", "user", "warehouse", "database") if not connect_kwargs.get(name)]
    if missing:
        raise IntegrationConfigError(
            f"Snowflake connection `{selected_name}` is missing required keys: {', '.join(missing)}"
        )
    return connect_kwargs


def drop_relations(connection: Any, schema: str, relation_names: list[str]) -> None:
    for relation_name in relation_names:
        qualified_name = f"{quote_identifier(schema)}.{quote_identifier(relation_name)}"
        _drop_relation_if_exists(connection, "view", qualified_name)
        _drop_relation_if_exists(connection, "table", qualified_name)


def drop_stages(connection: Any, schema: str, stage_names: list[str]) -> None:
    for stage_name in stage_names:
        qualified_name = f"{quote_identifier(schema)}.{quote_identifier(stage_name)}"
        _drop_relation_if_exists(connection, "stage", qualified_name)


def replace_csv_stage_from_file(connection: Any, schema: str, stage: str, csv_path: Path) -> None:
    qualified_stage = f"{quote_identifier(schema)}.{quote_identifier(stage)}"
    _execute(
        connection,
        (
            f"create or replace stage {qualified_stage} "
            "file_format = ("
            "type = csv "
            "field_delimiter = ',' "
            "skip_header = 0 "
            "field_optionally_enclosed_by = '\"'"
            ")"
        ),
    )
    _execute(
        connection,
        f"put '{csv_path.resolve().as_uri()}' @{qualified_stage} auto_compress=false overwrite=true",
    )


def create_target_table_from_spec(connection: Any, spec_path: Path, schema: str) -> str:
    spec = _load_spec(spec_path)
    target = spec["target"]
    table = _physical_name(target.get("table_name", target["id"]))
    columns = []
    for field in target["fields"]:
        columns.append(f"{quote_identifier(field['id'])} {field['data_type']}")
    if _surrogate_key_enabled(spec):
        columns.append(f"{quote_identifier(_surrogate_key_column(spec))} {SURROGATE_KEY_DATA_TYPE}")
    if _business_key_enabled(spec):
        columns.append(f"{quote_identifier(_business_key_column(spec))} {BUSINESS_KEY_DATA_TYPE}")
    for name, data_type in _generated_metadata_field_types_for_target(spec).items():
        columns.append(f"{quote_identifier(name)} {data_type}")
    ddl = f"create or replace table {quote_identifier(schema)}.{quote_identifier(table)} ({', '.join(columns)})"
    _execute(connection, ddl)
    return table


def replace_source_table_from_csv(connection: Any, schema: str, table: str, csv_path: Path) -> None:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise IntegrationConfigError(f"source table CSV must contain at least one row: {csv_path}")
    raw_columns = [str(column) for column in rows[0]]
    columns = [column.upper() for column in raw_columns]
    column_definitions = ", ".join(
        f"{quote_identifier(column)} varchar"
        for column in columns
    )
    _execute(
        connection,
        f"create or replace table {quote_identifier(schema)}.{quote_identifier(table)} ({column_definitions})",
    )
    sql = (
        f"insert into {quote_identifier(schema)}.{quote_identifier(table)} "
        f"({', '.join(quote_identifier(column) for column in columns)}) "
        f"values ({', '.join('%s' for _ in columns)})"
    )
    cursor = connection.cursor()
    try:
        for row in rows:
            cursor.execute(sql, tuple(row[column] for column in raw_columns))
    finally:
        cursor.close()


def replace_source_table_from_json(connection: Any, schema: str, table: str, json_path: Path) -> None:
    rows = _read_json_documents(json_path)
    if not rows:
        raise IntegrationConfigError(f"source JSON file must contain at least one document: {json_path}")
    _execute(
        connection,
        f"create or replace table {quote_identifier(schema)}.{quote_identifier(table)} (PAYLOAD variant)",
    )
    sql = f"insert into {quote_identifier(schema)}.{quote_identifier(table)} (PAYLOAD) select parse_json(%s)"
    cursor = connection.cursor()
    try:
        for row in rows:
            cursor.execute(sql, (json.dumps(row, separators=(",", ":")),))
    finally:
        cursor.close()


def _read_json_documents(json_path: Path) -> list[Any]:
    text = json_path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    parsed = json.loads(text)
    return parsed if isinstance(parsed, list) else [parsed]


def insert_csv_rows(connection: Any, schema: str, table: str, csv_path: Path, spec_path: Path) -> None:
    column_types = _target_column_types(spec_path)
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return
    columns = [str(column).upper() for column in rows[0]]
    select_list = ", ".join(
        f"cast(%s as {column_types[column]})"
        for column in columns
    )
    sql = (
        f"insert into {quote_identifier(schema)}.{quote_identifier(table)} "
        f"({', '.join(quote_identifier(column) for column in columns)}) "
        f"select {select_list}"
    )
    cursor = connection.cursor()
    try:
        for row in rows:
            cursor.execute(sql, tuple(row[column] for column in columns))
    finally:
        cursor.close()


def fetch_rows(
    connection: Any,
    schema: str,
    table: str,
    columns: list[str],
    spec_path: Path,
    order_by: list[str],
) -> list[dict[str, Any]]:
    column_types = _target_column_types(spec_path)
    select_list = ", ".join(_format_column(column, column_types[column]) for column in columns)
    order_clause = ""
    if order_by:
        order_clause = " order by " + ", ".join(quote_identifier(column) for column in order_by)
    cursor = connection.cursor()
    try:
        cursor.execute(f"select {select_list} from {quote_identifier(schema)}.{quote_identifier(table)}{order_clause}")
        names = [column[0].upper() for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def fetch_relation_rows(
    connection: Any,
    schema: str,
    table: str,
    columns: list[str],
    order_by: list[str],
) -> list[dict[str, Any]]:
    select_list = ", ".join(
        f"cast({quote_identifier(column)} as varchar) as {quote_identifier(column)}"
        for column in columns
    )
    order_clause = ""
    if order_by:
        order_clause = " order by " + ", ".join(quote_identifier(column) for column in order_by)
    cursor = connection.cursor()
    try:
        cursor.execute(f"select {select_list} from {quote_identifier(schema)}.{quote_identifier(table)}{order_clause}")
        names = [column[0].upper() for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def quote_identifier(value: str) -> str:
    return _physical_name(value)


def _format_column(column: str, data_type: str) -> str:
    quoted = quote_identifier(column)
    if data_type.lower().startswith("timestamp"):
        return f"to_varchar({quoted}::timestamp_ntz, 'YYYY-MM-DD HH24:MI:SS') as {quoted}"
    return f"cast({quoted} as varchar) as {quoted}"


def _target_column_types(spec_path: Path) -> dict[str, str]:
    spec = _load_spec(spec_path)
    column_types = {
        _physical_name(field["id"]): str(field["data_type"])
        for field in spec["target"]["fields"]
    }
    if _surrogate_key_enabled(spec):
        column_types[_surrogate_key_column(spec)] = SURROGATE_KEY_DATA_TYPE
    if _business_key_enabled(spec):
        column_types[_business_key_column(spec)] = BUSINESS_KEY_DATA_TYPE
    column_types.update(
        {
            _physical_name(name): data_type
            for name, data_type in _generated_metadata_field_types_for_target(spec).items()
        }
    )
    return column_types


def _generated_metadata_field_types_for_target(spec: dict[str, Any]) -> dict[str, str]:
    if _change_type(spec) in {"scd2_auto", "scd2_derived"}:
        return dict(GENERATED_METADATA_FIELD_TYPES)
    return {
        name: data_type
        for name, data_type in GENERATED_METADATA_FIELD_TYPES.items()
        if name.startswith("audit_")
    }


def _load_spec(spec_path: Path) -> dict[str, Any]:
    with spec_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise IntegrationConfigError(f"spec is not a mapping: {spec_path}")
    return data


def _physical_name(value: Any) -> str:
    return str(value).upper()


def _change_type(spec: dict[str, Any]) -> str | None:
    control_data = spec.get("control_data", {})
    if not isinstance(control_data, dict):
        return None
    value = control_data.get("change_type")
    return str(value) if value is not None else None


def _business_key_column(spec: dict[str, Any]) -> str:
    target = spec.get("target", {})
    target_id = target.get("id") if isinstance(target, dict) else None
    return _physical_name(f"{_target_key_base(target_id)}_business_key" if isinstance(target_id, str) else "business_key")


def _business_key_enabled(spec: dict[str, Any]) -> bool:
    control_data = spec.get("control_data", {})
    if not isinstance(control_data, dict):
        return True
    return control_data.get("skip_business_key") is not True


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


def _default_connection_name(config: dict[str, Any], connections: dict[str, Any]) -> str:
    del config
    if DEFAULT_SNOWFLAKE_CONNECTION in connections:
        return DEFAULT_SNOWFLAKE_CONNECTION
    available = ", ".join(sorted(str(name) for name in connections))
    raise IntegrationConfigError(
        f"Snowflake connection profile `{DEFAULT_SNOWFLAKE_CONNECTION}` was not found. "
        f"Add [connections.{DEFAULT_SNOWFLAKE_CONNECTION}] to ~/.snowflake/config.toml, "
        f"or set TMS_SNOWFLAKE_CONNECTION to one of: {available}"
    )


def _normalise_snowflake_connection(raw_connection: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "accountname": "account",
        "username": "user",
        "dbname": "database",
        "warehousename": "warehouse",
        "rolename": "role",
    }
    allowed_keys = {
        "account",
        "user",
        "password",
        "warehouse",
        "database",
        "schema",
        "role",
        "authenticator",
        "host",
        "port",
        "protocol",
        "region",
        "passcode",
        "passcode_in_password",
        "private_key_file",
        "private_key_file_pwd",
        "token",
        "client_store_temporary_credential",
        "client_request_mfa_token",
        "login_timeout",
        "network_timeout",
        "session_parameters",
    }
    normalised: dict[str, Any] = {}
    for key, value in raw_connection.items():
        connector_key = aliases.get(str(key), str(key))
        if connector_key in allowed_keys:
            normalised[connector_key] = value
    return normalised


def _execute(connection: Any, sql: str) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(sql)
    finally:
        cursor.close()


def _drop_relation_if_exists(connection: Any, relation_type: str, qualified_name: str) -> None:
    try:
        _execute(connection, f"drop {relation_type} if exists {qualified_name}")
    except snowflake.connector.errors.ProgrammingError as exc:
        if getattr(exc, "errno", None) != 2203:
            raise
