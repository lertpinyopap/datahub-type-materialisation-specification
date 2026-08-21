# scd2_derived_out_of_order_multi_history

This scenario verifies that `scd2_derived` can load customer states out of
chronological order and still produce continuous gold SCD2 windows.

The source loads Gold first, then backfills Bronze, then inserts Silver between
Bronze and Gold.

## Checks

- Loading a newer state first creates one current gold row.
- Backfilling an older state closes it immediately before the existing newer state.
- Loading the middle state later recalculates the affected customer's full window.
- Unaffected customers are not present in this scenario, keeping the window behavior isolated.

