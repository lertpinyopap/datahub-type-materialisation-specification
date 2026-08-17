# SCD1 Table Source JSON Flatten

This scenario proves that a generated dbt project can materialise values from a
Snowflake table containing loaded JSON in a `VARIANT` column.

The live runner loads `data/load_001_source.json` into a source table with a
single `PAYLOAD variant` column. The generated source model extracts scalar
values from nested Snowflake paths as varchar source values, flattens the
`customer.orders` array with `lateral flatten`, and lets the normal TMS
transform and validation pipeline cast those values into the target table.

The JSON fixture includes nested account/profile fields, an orders array, a
numeric amount, a date string, and a boolean flag.
