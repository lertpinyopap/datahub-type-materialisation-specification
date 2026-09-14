{% macro tms_incremental_bookmark_predicate(source_timestamp, watermark_filter=none) -%}
    {{ source_timestamp }} > coalesce((
        select LAST_SOURCE_TIMESTAMP from __BOOKMARK_RELATION__
        where PIPELINE_NAME = __PIPELINE_NAME_LITERAL__
          and SOURCE_RELATION = __SOURCE_RELATION_LITERAL__
    ), '1900-01-01'::timestamp_ltz)
    and {{ source_timestamp }} <= (
        select max(__SOURCE_TIMESTAMP_COLUMN__) from __SOURCE_RELATION__{% if watermark_filter %} where {{ watermark_filter }}{% endif %}
    )
{%- endmacro %}
