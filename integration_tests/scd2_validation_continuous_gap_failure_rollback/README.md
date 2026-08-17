# SCD2 continuous gap failure rollback

Proves that `scd2_validation: continuous` rejects a candidate target history
that leaves a gap between neighbouring versions for a business key. The dbt
build must fail and leave the existing target unchanged.
