{% macro tms_job_relation_label() -%}
{% set tms_job_database = __JOB_DATABASE_EXPRESSION__ %}
{% set tms_job_schema = var('tms_job_schema', __JOB_SCHEMA_DEFAULT_LITERAL__) | upper %}
{{ return(tms_job_database ~ '.' ~ tms_job_schema ~ '.' ~ __JOB_TABLE_LITERAL__) }}
{%- endmacro %}

{% macro tms_job_create() -%}
{{ log('TMS hook: ensuring job-event table exists: ' ~ tms_job_relation_label(), info=true) }}
create table if not exists __JOB_RELATION__ (
    JOB_ID varchar(64),
    EVENT_TYPE varchar(32),
    EVENT_TIMESTAMP __JOB_TIMESTAMP_DATA_TYPE__,
    RESULT varchar(64),
    DETAILS varchar(16777216),
    SPEC_FILE_NAME varchar(1024),
    GENERATED_TABLE varchar(1024),
    QUARANTINE_TABLE varchar(1024),
    LOADED_COUNT number(38, 0),
    QUARANTINE_COUNT number(38, 0),
    AUDIT_DATA_PROCESS_KEY __AUDIT_DATA_PROCESS_KEY_TYPE__
)
{%- endmacro %}

{% macro tms_job_start() -%}
{{ log('TMS hook: recording JOB_START event: ' ~ tms_job_relation_label(), info=true) }}
insert into __JOB_RELATION__
    (JOB_ID, EVENT_TYPE, EVENT_TIMESTAMP, RESULT, DETAILS, SPEC_FILE_NAME, GENERATED_TABLE,
     QUARANTINE_TABLE, LOADED_COUNT, QUARANTINE_COUNT, AUDIT_DATA_PROCESS_KEY)
select
    __JOB_ID_EXPRESSION__,
    'JOB_START',
    cast(current_timestamp() as __JOB_TIMESTAMP_DATA_TYPE__),
    null,
    null,
    cast(__SPEC_FILE_NAME_LITERAL__ as varchar(1024)),
    cast(__GENERATED_TABLE_LITERAL__ as varchar(1024)),
    __QUARANTINE_TABLE_EXPRESSION__,
    cast(null as number(38, 0)),
    cast(null as number(38, 0)),
    cast('{{ var("audit_data_process_key", "manual") }}' as __AUDIT_DATA_PROCESS_KEY_TYPE__)
{%- endmacro %}

{% macro tms_job_end() -%}
{% set failed_result_count =
    (results | selectattr('status', 'equalto', 'error') | list | length) +
    (results | selectattr('status', 'equalto', 'fail') | list | length) %}
{% set validation_guard_failed = namespace(value=false) %}
{% for result in results %}
{% if result.status in ['error', 'fail'] and result.node.name == __VALIDATION_GUARD_MODEL_NAME_LITERAL__ %}{% set validation_guard_failed.value = true %}{% endif %}
{% if result.status in ['error', 'fail'] and 'TYPE_MATERIALISATION_VALIDATION_FAILED' in (result.message | string) %}{% set validation_guard_failed.value = true %}{% endif %}
{% endfor %}
{% set explicit_job_details = var("job_details", none) %}
{{ log('TMS hook: recording JOB_END event: ' ~ tms_job_relation_label(), info=true) }}
__GENERATED_RELATION_LOOKUP__
__QUARANTINE_RELATION_LOOKUP__
insert into __JOB_RELATION__
    (JOB_ID, EVENT_TYPE, EVENT_TIMESTAMP, RESULT, DETAILS, SPEC_FILE_NAME, GENERATED_TABLE,
     QUARANTINE_TABLE, LOADED_COUNT, QUARANTINE_COUNT, AUDIT_DATA_PROCESS_KEY)
with
    loaded_counts as (select {% if tms_generated_relation is not none %}(select count(*) from {{ tms_generated_relation }}){% else %}null{% endif %} as LOADED_COUNT),
    quarantine_counts as (select {% if tms_quarantine_relation is not none %}(select count(*) from {{ tms_quarantine_relation }}){% else %}null{% endif %} as QUARANTINE_COUNT)
select
    __JOB_ID_EXPRESSION__,
    'JOB_END',
    cast(current_timestamp() as __JOB_TIMESTAMP_DATA_TYPE__),
    {% if failed_result_count > 0 %}'FAILED'{% else %}case when quarantine_counts.QUARANTINE_COUNT > 0 then 'COMPLETED_WITH_QUARANTINE' else 'COMPLETED' end{% endif %},
    {% if explicit_job_details is not none %}'{{ explicit_job_details | replace("'", "''") }}'{% elif failed_result_count > 0 and validation_guard_failed.value %}'validation errors failed the load'{% elif failed_result_count > 0 %}'dbt run failed; inspect dbt artifacts for runtime details'{% else %}case when quarantine_counts.QUARANTINE_COUNT > 0 then 'validation errors written to quarantine output' else null end{% endif %},
    cast(__SPEC_FILE_NAME_LITERAL__ as varchar(1024)),
    cast(__GENERATED_TABLE_LITERAL__ as varchar(1024)),
    __QUARANTINE_TABLE_EXPRESSION__,
    loaded_counts.LOADED_COUNT,
    quarantine_counts.QUARANTINE_COUNT,
    cast('{{ var("audit_data_process_key", "manual") }}' as __AUDIT_DATA_PROCESS_KEY_TYPE__)
from loaded_counts cross join quarantine_counts
{%- endmacro %}
