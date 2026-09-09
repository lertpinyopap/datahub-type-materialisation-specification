from pathlib import Path

from type_materialisation.dbt_generate import GenerateDbtOptions, generate_dbt_project
from type_materialisation.spec import validate_semantics


def test_target_and_field_tags_generate_snowflake_post_hooks(tmp_path: Path) -> None:
    spec = {
        "id": "tagged_table",
        "control_data": {
            "change_type": "scd1",
            "business_key": {"fields": ["FIRST_NAME"]},
        },
        "source": {
            "format": "table",
            "query": "select first_name, birth_date from raw.customer",
        },
        "target": {
            "id": "customer",
            "schema": "business",
            "fields": [
                {
                    "id": "FIRST_NAME",
                    "source": {"column": "first_name"},
                    "data_type": "varchar(100)",
                    "nullable": True,
                    "tags": {
                        "PII_CATEGORY": "IDENTIFIER",
                        "PCI_CATEGORY": "PCI",
                    },
                }
            ],
        },
    }

    assert validate_semantics(spec, abstract=False) == []
    result = generate_dbt_project(
        GenerateDbtOptions(
            spec_path=tmp_path / "tagged.yaml",
            output_dir=tmp_path / "generated",
            spec=spec,
        )
    )

    assert result.errors == []
    final_sql = (tmp_path / "generated" / "models" / "generated" / "customer.sql").read_text()
    assert 'alter table {{ this }} modify column FIRST_NAME set tag PII_CATEGORY = \'IDENTIFIER\'' in final_sql
    assert 'alter table {{ this }} modify column FIRST_NAME set tag PCI_CATEGORY = \'PCI\'' in final_sql


def test_apply_governance_tag_application_generates_procedure_post_hook(tmp_path: Path) -> None:
    spec = {
        "id": "governed_table",
        "control_data": {
            "change_type": "scd1",
            "business_key": {"fields": ["FIRST_NAME"]},
        },
        "source": {
            "format": "table",
            "query": "select first_name from raw.customer",
        },
        "target": {
            "id": "customer",
            "schema": "business",
            "tag_application": "apply_governance",
            "apply_governance_procedure": "NONPROD_GOVERNANCE.OVERRIDES.APPLY_GOVERNANCE",
            "governance_contract": {
                "table": "NONPROD_GOVERNANCE.METADATA.CONTRACT_COLUMNS",
                "source": "dbt:card_customer",
                "version": "1",
            },
            "fields": [
                {
                    "id": "FIRST_NAME",
                    "source": {"column": "first_name"},
                    "data_type": "varchar(100)",
                    "nullable": True,
                    "tags": {"PII_CATEGORY": "IDENTIFIER"},
                    "description": "Customer first name",
                }
            ],
        },
    }

    assert validate_semantics(spec, abstract=False) == []
    result = generate_dbt_project(
        GenerateDbtOptions(
            spec_path=tmp_path / "governed.yaml",
            output_dir=tmp_path / "generated",
            spec=spec,
        )
    )

    assert result.errors == []
    final_sql = (tmp_path / "generated" / "models" / "generated" / "customer.sql").read_text()
    assert (
        '"call NONPROD_GOVERNANCE.OVERRIDES.APPLY_GOVERNANCE(\'{{ this.database | upper }}\', '
        "'{{ this.schema | upper }}', '{{ this.identifier | upper }}')\""
        in final_sql
    )
    assert "merge into NONPROD_GOVERNANCE.METADATA.CONTRACT_COLUMNS as t" in final_sql
    assert "'FIRST_NAME' as column_name" in final_sql
    assert "'IDENTIFIER' as pii_category" in final_sql
    assert "null as pci_category" in final_sql
    assert "'Customer first name' as description" in final_sql
    assert final_sql.index("merge into NONPROD_GOVERNANCE.METADATA.CONTRACT_COLUMNS as t") < final_sql.index(
        "call NONPROD_GOVERNANCE.OVERRIDES.APPLY_GOVERNANCE"
    )
    assert "alter table {{ this }} modify column FIRST_NAME set tag" not in final_sql
