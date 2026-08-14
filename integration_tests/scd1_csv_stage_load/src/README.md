# Scenario Source

`assertions.py` demonstrates a scenario-specific assertion hook.

The shared runner calls `assert_after_load(context)` after the normal expected
CSV checks for each load. This scenario uses that hook to run a deliberately
trivial SQL aggregate check against known test setup data. It is included as an
example of the hook pattern, not because the aggregate itself is an important
staged CSV behavior.
