from __future__ import annotations


def assert_after_load(context) -> None:
    """Example-only custom assertion hook.

    This is deliberately a trivial assertion over test setup data. It exists to
    demonstrate the `src/assertions.py` pattern, not to encode an important
    business invariant for staged CSV loads.
    """
    if context.load_name != "load_001":
        return

    qualified_table = f"{context.target_schema}.{context.target_table}"
    cursor = context.connection.cursor()
    try:
        cursor.execute(
            f"select count(*) as ROW_COUNT, sum(ACCOUNT_PRIORITY) as PRIORITY_TOTAL from {qualified_table}"
        )
        row_count, priority_total = cursor.fetchone()
    finally:
        cursor.close()

    assert int(row_count) == 2
    assert int(priority_total) == 30
