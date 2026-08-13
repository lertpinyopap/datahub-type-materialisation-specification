import re
from typing import Any

from type_materialisation.macro_api import TypeMaterialisationMacro

ACCOUNT_NUMBER_RE = re.compile(r"^ACCT[0-9]{12}$")


class ValidateAccountNumber(TypeMaterialisationMacro):
    supports_python_execution = True

    def generate_dbt_macro(self) -> str:
        return """
{% macro validate_account_number(column_expression) %}
    case
        when regexp_like({{ column_expression }}, '^ACCT[0-9]{12}$')
         and {{ column_expression }} <> 'ACCT000000000000'
            then null
        else 'account number must use ACCT followed by 12 non-zero-sequence digits'
    end
{% endmacro %}
""".strip()

    def execute(self, *, value: Any, **_context: Any) -> str | None:
        text = str(value)
        if ACCOUNT_NUMBER_RE.fullmatch(text) is None:
            return "account number must use ACCT followed by 12 digits"
        if text == "ACCT000000000000":
            return "account number sequence must be greater than zero"
        return None


class NormaliseAccountName(TypeMaterialisationMacro):
    def generate_dbt_macro(self) -> str:
        return """
{% macro normalise_account_name(column_expression) %}
    nullif(upper(trim({{ column_expression }})), '')
{% endmacro %}
""".strip()


validate_account_number = ValidateAccountNumber()
normalise_account_name = NormaliseAccountName()
