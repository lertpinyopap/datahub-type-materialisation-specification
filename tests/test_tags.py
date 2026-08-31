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
