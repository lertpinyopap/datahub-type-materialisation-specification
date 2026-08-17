# SCD2 manual multiple current failure rollback

Proves that manual SCD2 validates the candidate target history after applying
upsert semantics. The load would leave two current rows for the same business
key, so the dbt build must fail and leave the existing target unchanged.
