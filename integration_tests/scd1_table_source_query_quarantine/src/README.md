# Scenario Source

This scenario does not need custom SQL or Python. The shared live runner creates
the backing source table from `data/load_001_source.csv`; the spec's
`source.query` then shapes the row set used by generated dbt models.
