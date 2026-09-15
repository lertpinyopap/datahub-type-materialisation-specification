{% set tms_bookmark_relation_label = __BOOKMARK_RELATION_LABEL_EXPRESSION__ %}
{% macro tms_bookmark_create() -%}
{{ log('TMS hook: ensuring incremental bookmark table exists: ' ~ tms_bookmark_relation_label, info=true) }}
create table if not exists __BOOKMARK_RELATION__ (
    PIPELINE_NAME varchar not null,
    SOURCE_RELATION varchar not null,
    LAST_SOURCE_TIMESTAMP timestamp_ntz,
    UPDATED_AT timestamp_ntz not null default current_timestamp(),
    UPDATED_BY varchar not null default current_role()
)
{%- endmacro %}

{% macro tms_bookmark_advance() -%}
{% set tms_bookmark_failed_count =
    (results | selectattr('status', 'equalto', 'error') | list | length) +
    (results | selectattr('status', 'equalto', 'fail') | list | length) %}
{{ log('TMS hook: advancing incremental bookmark: ' ~ tms_bookmark_relation_label, info=true) }}
{% if tms_bookmark_failed_count == 0 %}
merge into __BOOKMARK_RELATION__ as target using (
    select __PIPELINE_NAME_LITERAL__ as PIPELINE_NAME,
           __SOURCE_RELATION_LITERAL__ as SOURCE_RELATION,
           max(__SOURCE_TIMESTAMP_COLUMN__) as LAST_SOURCE_TIMESTAMP
    from __SOURCE_RELATION__
    having max(__SOURCE_TIMESTAMP_COLUMN__) is not null
) as source on target.PIPELINE_NAME = source.PIPELINE_NAME
    and target.SOURCE_RELATION = source.SOURCE_RELATION
when matched then update set LAST_SOURCE_TIMESTAMP = source.LAST_SOURCE_TIMESTAMP,
    UPDATED_AT = current_timestamp()::timestamp_ntz, UPDATED_BY = current_role()
when not matched then insert (PIPELINE_NAME, SOURCE_RELATION, LAST_SOURCE_TIMESTAMP, UPDATED_AT, UPDATED_BY)
    values (source.PIPELINE_NAME, source.SOURCE_RELATION, source.LAST_SOURCE_TIMESTAMP,
            current_timestamp()::timestamp_ntz, current_role())
{% else %}
select 1 where false
{% endif %}
{%- endmacro %}
