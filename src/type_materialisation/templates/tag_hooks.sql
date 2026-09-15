{% macro __TAG_HOOK_MACRO_NAME__() -%}
{% set tms_tag_statement %}
__TAG_HOOK_STATEMENT__
{% endset %}
{{ log('__TAG_HOOK_LOG_MESSAGE__', info=true) }}
{{ tms_tag_statement }}
{%- endmacro %}
