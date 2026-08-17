from __future__ import annotations

import re


EXPECTED_A1_BUSINESS_KEY = "16a36e86f6fed5d465ff332511a0ce1a863b55d364b25a7cdaa25db19abf9648"
INITIAL_SURROGATE_KEY = "00000000-0000-4000-8000-000000000001"
UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def assert_after_load(context) -> None:
    surrogate_column = "ACCOUNT_KEY"
    business_column = "ACCOUNT_BUSINESS_KEY"
    qualified_table = f"{context.target_schema}.{context.target_table}"
    cursor = context.connection.cursor()
    try:
        cursor.execute(
            f"""
            select
                cast(ACCOUNT_ID as varchar) as ACCOUNT_ID,
                cast({surrogate_column} as varchar) as SURROGATE_KEY,
                cast({business_column} as varchar) as BUSINESS_KEY,
                cast(VALID_FROM_DATETIME as varchar) as VALID_FROM_DATETIME
            from {qualified_table}
            order by ACCOUNT_ID, VALID_FROM_DATETIME
            """
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()

    assert rows
    surrogate_keys: list[str] = []
    initial_surrogate_key_seen = False
    for account_id, surrogate_key, business_key, valid_from_datetime in rows:
        assert account_id == "A1"
        assert business_key == EXPECTED_A1_BUSINESS_KEY
        assert surrogate_key is not None
        assert UUID_PATTERN.match(str(surrogate_key)) is not None
        surrogate_keys.append(str(surrogate_key))

        if str(valid_from_datetime).startswith("0001-01-01"):
            assert surrogate_key == INITIAL_SURROGATE_KEY
            initial_surrogate_key_seen = True

    assert len(surrogate_keys) == len(set(surrogate_keys))
    assert initial_surrogate_key_seen
