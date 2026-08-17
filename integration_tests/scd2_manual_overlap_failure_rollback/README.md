# SCD2 manual overlap failure rollback

Proves that manual SCD2 validates validity windows after applying upsert
semantics. The load replaces the existing row but creates overlapping incoming
windows for the same business key, so the dbt build must fail and leave the
existing target unchanged.
