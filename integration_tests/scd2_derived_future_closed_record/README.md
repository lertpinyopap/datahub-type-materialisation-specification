# scd2_derived_future_closed_record

This scenario verifies the `previous_and_next` affected-window behavior for a
row inserted between two existing customer versions.

The first load creates Bronze and Gold versions. The second load inserts Silver
between them. The target should recalculate the previous Bronze end date and use
the existing future Gold row to close Silver.

## Checks

- The initial load builds a two-row customer history.
- A later source row can be inserted between existing versions.
- The inserted row receives a closed `VALID_TO_DATETIME`, not an open-ended one.
- The future Gold row remains current.

