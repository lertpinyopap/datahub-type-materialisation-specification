from __future__ import annotations

import csv
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import snowflake.connector
import yaml

from type_materialisation.spec import GENERATED_METADATA_FIELD_TYPES


class IntegrationConfigError(RuntimeError):
    """Raised when live database configuration is incomplete."""


def live_database_enabled() -> bool:
    return os.environ.get("TMS_RUN_INTEGRATION") == "1"


@contextmanager
def snowflake_connection() -> Iterator[Any]:
    required = [
        "TMS_SNOWFLAKE_ACCOUNT",
        "TMS_SNOWFLAKE_USER",
        "TMS_SNOWFLAKE_WAREHOUSE",
        "TMS_SNOWFLAKE_DATABASE",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise IntegrationConfigError(f"missing live integration environment variables: {', '.join(missing)}")
    connect_kwargs: dict[str, Any] = {
        "account": os.environ["TMS_SNOWFLAKE_ACCOUNT"],
        "user": os.environ["TMS_SNOWFLAKE_USER"],
        "warehouse": os.environ["TMS_SNOWFLAKE_WAREHOUSE"],
        "database": os.environ["TMS_SNOWFLAKE_DATABASE"],
    }
    if os.environ.get("TMS_SNOWFLAKE_PASSWORD"):
        connect_kwargs["password"] = os.environ["TMS_SNOWFLAKE_PASSWORD"]
    if os.environ.get("TMS_SNOWFLAKE_AUTHENTICATOR"):
        connect_kwargs["authenticator"] = os.environ["TMS_SNOWFLAKE_AUTHENTICATOR"]
    if os.environ.get("TMS_SNOWFLAKE_ROLE"):
        connect_kwargs["role"] = os.environ["TMS_SNOWFLAKE_ROLE"]
    connection = snowflake.connector.connect(**connect_kwargs)
    try:
        yield connection
    finally:
        connection.close()


def create_schema(connection: Any, schema: str) -> None:
    _execute(connection, f"create schema if not exists {quote_identifier(schema)}")


def drop_relations(connection: Any, schema: str, relation_names: list[str]) -> None:
    for relation_name in relation_names:
        qualified_name = f"{quote_identifier(schema)}.{quote_identifier(relation_name)}"
        _execute(connection, f"drop view if exists {qualified_name}")
        _execute(connection, f"drop table if exists {qualified_name}")


def create_target_table_from_spec(connection: Any, spec_path: Path, schema: str) -> str:
    spec = _load_spec(spec_path)
    target = spec["target"]
    table = _physical_name(target.get("table_name", target["id"]))
    columns = []
    for field in target["fields"]:
        columns.append(f"{quote_identifier(field['id'])} {field['data_type']}")
    for name, data_type in GENERATED_METADATA_FIELD_TYPES.items():
        columns.append(f"{quote_identifier(name)} {data_type}")
    ddl = f"create or replace table {quote_identifier(schema)}.{quote_identifier(table)} ({', '.join(columns)})"
    _execute(connection, ddl)
    return table


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
    values = [tuple(row[column] for column in row) for row in rows]
    cursor = connection.cursor()
    try:
        cursor.executemany(sql, values)
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
    column_types.update({_physical_name(name): data_type for name, data_type in GENERATED_METADATA_FIELD_TYPES.items()})
    return column_types


def _load_spec(spec_path: Path) -> dict[str, Any]:
    with spec_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise IntegrationConfigError(f"spec is not a mapping: {spec_path}")
    return data


def _physical_name(value: Any) -> str:
    return str(value).upper()


def _execute(connection: Any, sql: str) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(sql)
    finally:
        cursor.close()
