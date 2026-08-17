This scenario has no custom Python assertions. The expected target CSV asserts
that changing only the excluded `source_batch_id` field does not create a new
SCD2 version and that `BUSINESS_DATA_HASH` remains SHA2-256 of `A1|1`.
