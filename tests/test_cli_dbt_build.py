import json
import subprocess
from pathlib import Path

from type_materialisation import cli
from type_materialisation.csv_validate import CsvValidationResult
from type_materialisation.dbt_generate import DbtGenerationResult


def test_dbt_build_uses_default_project_dir_and_passes_through_options(monkeypatch, tmp_path: Path) -> None:
    spec_path = tmp_path / "account.yaml"
    spec_path.write_text("id: account\n", encoding="utf-8")
    commands: list[list[str]] = []

    monkeypatch.setattr(cli.shutil, "which", lambda executable: "/usr/local/bin/dbt" if executable == "dbt" else None)

    def fake_run(command, *, check):
        commands.append(command)
        assert check is False
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli.main(
        [
            "dbt-build",
            "--spec",
            str(spec_path),
            "--target",
            "dev",
            "--vars",
            "{target_schema: TMP, tms_job_schema: TMP}",
        ]
    )

    assert result == 0
    assert commands == [
        [
            "/usr/local/bin/dbt",
            "build",
            "--project-dir",
            str(Path("tmp") / "account"),
            "--target",
            "dev",
            "--vars",
            "{target_schema: TMP, tms_job_schema: TMP}",
        ]
    ]


def test_generate_dbt_uses_same_default_project_dir_as_dbt_build(monkeypatch, tmp_path: Path) -> None:
    spec_path = tmp_path / "account.yaml"
    spec_path.write_text("id: account\n", encoding="utf-8")
    output_dirs: list[Path] = []

    monkeypatch.setattr(cli, "resolve_and_validate_spec", lambda *args, **kwargs: ({}, []))

    def fake_generate(options):
        output_dirs.append(options.output_dir)
        return DbtGenerationResult(output_dir=options.output_dir)

    monkeypatch.setattr(cli, "generate_dbt_project", fake_generate)

    result = cli.main(["generate-dbt", "--spec", str(spec_path)])

    assert result == 0
    assert output_dirs == [Path("tmp") / "account"]
    assert output_dirs == [cli._default_dbt_project_dir(spec_path)]


def test_generate_dbt_passes_python_vars_to_generator(monkeypatch, tmp_path: Path) -> None:
    spec_path = tmp_path / "account.yaml"
    spec_path.write_text("id: account\n", encoding="utf-8")
    captured_vars: list[dict] = []

    monkeypatch.setattr(cli, "resolve_and_validate_spec", lambda *args, **kwargs: ({}, []))

    def fake_generate(options):
        captured_vars.append(options.vars)
        return DbtGenerationResult(output_dir=options.output_dir)

    monkeypatch.setattr(cli, "generate_dbt_project", fake_generate)

    result = cli.main(["generate-dbt", "--spec", str(spec_path), "--vars", "{seed_file: account.csv}"])

    assert result == 0
    assert captured_vars == [{"seed_file": "account.csv"}]


def test_validate_resolves_input_file_python_vars(monkeypatch, tmp_path: Path) -> None:
    spec_path = tmp_path / "account.yaml"
    spec_path.write_text("id: account\n", encoding="utf-8")
    csv_path = tmp_path / "account.csv"
    csv_path.write_text("account_id\nA1\n", encoding="utf-8")
    captured_paths: list[Path] = []

    monkeypatch.setattr(cli, "resolve_and_validate_spec", lambda *args, **kwargs: ({}, []))

    def fake_validate(_spec_path, input_file, **_kwargs):
        captured_paths.append(input_file)
        return CsvValidationResult(rows_checked=1)

    monkeypatch.setattr(cli, "validate_csv_file", fake_validate)

    result = cli.main(
        [
            "validate",
            "--spec",
            str(spec_path),
            "--input-file",
            "{{ tms_var('input_file') }}",
            "--vars",
            f"{{input_file: {csv_path}}}",
        ]
    )

    assert result == 0
    assert captured_paths == [csv_path]


def test_validate_defaults_to_resolved_seed_file(monkeypatch, tmp_path: Path) -> None:
    spec_path = tmp_path / "account.yaml"
    spec_path.write_text("id: account\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    seed_path = run_dir / "account.csv"
    seed_path.write_text("account_id\nA1\n", encoding="utf-8")
    captured_paths: list[Path] = []
    spec = {
        "source": {
            "format": "csv",
            "load_method": "dbt_seed",
            "seed": {
                "file": "account.csv",
            },
        }
    }

    monkeypatch.setattr(cli, "resolve_and_validate_spec", lambda *args, **kwargs: (spec, []))
    monkeypatch.chdir(run_dir)

    def fake_validate(_spec_path, input_file, **_kwargs):
        captured_paths.append(input_file)
        return CsvValidationResult(rows_checked=1)

    monkeypatch.setattr(cli, "validate_csv_file", fake_validate)

    result = cli.main(
        [
            "validate",
            "--spec",
            str(spec_path),
            "--vars",
            "{seed_file: account.csv}",
        ]
    )

    assert result == 0
    assert captured_paths == [seed_path]


def test_validate_resolves_spec_tms_vars_before_validation(monkeypatch, tmp_path: Path) -> None:
    spec_path = tmp_path / "account.yaml"
    spec_path.write_text(
        """
id: account_csv
control_data:
  change_type: scd1
  business_key:
    fields:
      - account_id
source:
  format: csv
  header: true
  load_method: dbt_seed
  seed:
    file: "{{ tms_var('seed_file') }}"
target:
  id: account
  schema: "{{ tms_var('target_schema', 'business') }}"
  fields:
    - id: account_id
      source:
        pos: 0
        column: account_id
      data_type: varchar(20)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    seed_path = run_dir / "account.csv"
    seed_path.write_text("account_id\nA1\n", encoding="utf-8")
    monkeypatch.chdir(run_dir)

    result = cli.main(
        [
            "validate",
            "--spec",
            str(spec_path),
            "--vars",
            "{seed_file: account.csv}",
        ]
    )

    assert result == 0


def test_parse_fails_when_required_tms_var_is_missing(tmp_path: Path) -> None:
    spec_path = tmp_path / "account.yaml"
    spec_path.write_text(
        """
id: account_csv
control_data:
  change_type: scd1
  business_key:
    fields:
      - account_id
source:
  format: csv
  header: true
target:
  id: account
  schema: "{{ tms_var('target_schema') }}"
  fields:
    - id: account_id
      source:
        pos: 0
        column: account_id
      data_type: varchar(20)
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = cli.main(["parse", "--spec", str(spec_path)])

    assert result == 1


def test_validate_requires_input_file_for_non_seed_sources(monkeypatch, tmp_path: Path) -> None:
    spec_path = tmp_path / "account.yaml"
    spec_path.write_text("id: account\n", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "resolve_and_validate_spec",
        lambda *args, **kwargs: ({"source": {"format": "csv"}}, []),
    )

    result = cli.main(["validate", "--spec", str(spec_path)])

    assert result == 1


def test_dbt_build_allows_project_dir_override_and_returns_dbt_exit_code(monkeypatch, tmp_path: Path) -> None:
    spec_path = tmp_path / "account.yaml"
    project_dir = tmp_path / "dbt-account"
    spec_path.write_text("id: account\n", encoding="utf-8")
    commands: list[list[str]] = []

    monkeypatch.setattr(cli.shutil, "which", lambda executable: "/usr/local/bin/dbt" if executable == "dbt" else None)

    def fake_run(command, *, check):
        commands.append(command)
        assert check is False
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli.main(["dbt-build", "--spec", str(spec_path), "--project-dir", str(project_dir), "--target", "prod"])

    assert result == 7
    assert commands == [["/usr/local/bin/dbt", "build", "--project-dir", str(project_dir), "--target", "prod"]]


def test_dbt_build_runs_dbt_build(monkeypatch, tmp_path: Path) -> None:
    spec_path = tmp_path / "account.yaml"
    project_dir = tmp_path / "dbt-account"
    spec_path.write_text("id: account\n", encoding="utf-8")
    commands: list[list[str]] = []

    monkeypatch.setattr(cli.shutil, "which", lambda executable: "/usr/local/bin/dbt" if executable == "dbt" else None)

    def fake_run(command, *, check):
        commands.append(command)
        assert check is False
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli.main(
        [
            "dbt-build",
            "--spec",
            str(spec_path),
            "--project-dir",
            str(project_dir),
            "--target",
            "dev",
            "--vars",
            "{target_schema: TMP, tms_job_schema: TMP}",
        ]
    )

    assert result == 0
    assert commands == [
        [
            "/usr/local/bin/dbt",
            "build",
            "--project-dir",
            str(project_dir),
            "--target",
            "dev",
            "--vars",
            "{target_schema: TMP, tms_job_schema: TMP}",
        ],
    ]


def test_dbt_build_uses_env_dbt_path(monkeypatch, tmp_path: Path) -> None:
    spec_path = tmp_path / "account.yaml"
    project_dir = tmp_path / "dbt-account"
    spec_path.write_text("id: account\n", encoding="utf-8")
    commands: list[list[str]] = []

    monkeypatch.setenv("DBT_EXECUTABLE_PATH", "/opt/dbt/bin/dbt")

    def fake_run(command, *, check):
        commands.append(command)
        assert check is False
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli.main(
        [
            "dbt-build",
            "--spec",
            str(spec_path),
            "--project-dir",
            str(project_dir),
            "--target",
            "dev",
        ]
    )

    assert result == 0
    assert commands == [
        [
            "/opt/dbt/bin/dbt",
            "build",
            "--project-dir",
            str(project_dir),
            "--target",
            "dev",
        ],
    ]


def test_row_summary_command_reports_only_target_models(tmp_path: Path, capsys) -> None:
    project_dir = tmp_path / "generated"
    target_dir = project_dir / "target"
    target_dir.mkdir(parents=True)
    (target_dir / "manifest.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "model.generated.customer__source": {
                        "resource_type": "model",
                        "name": "customer__source",
                        "alias": "CUSTOMER__SOURCE",
                    },
                    "model.generated.customer__validation_guard": {
                        "resource_type": "model",
                        "name": "customer__validation_guard",
                        "alias": "CUSTOMER__VALIDATION_GUARD",
                    },
                    "model.generated.customer": {
                        "resource_type": "model",
                        "name": "customer",
                        "alias": "CUSTOMER",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    (target_dir / "run_results.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "unique_id": "model.generated.customer__source",
                        "relation_name": "DB.INTERMEDIATE.CUSTOMER__SOURCE",
                        "status": "success",
                        "adapter_response": {"rows_affected": 1},
                    },
                    {
                        "unique_id": "model.generated.customer",
                        "relation_name": "DB.CORE.CUSTOMER",
                        "status": "success",
                        "execution_time": 7.248287916183472,
                        "adapter_response": {
                            "rows_affected": 234450,
                            "query_id": "01c698a9-3206-c515-0002-4dfa03af4416",
                        },
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = cli.main(["row-summary", "--project-dir", str(project_dir)])

    output = capsys.readouterr().out
    assert result == 0
    assert "TMS dbt write summary:" in output
    assert "DB.CORE.CUSTOMER: status=success, rows_affected=234450" in output
    assert "duration_seconds=7.25" in output
    assert "query_id=01c698a9-3206-c515-0002-4dfa03af4416" in output
    assert "business-change counts" in output
    assert "DB.INTERMEDIATE.CUSTOMER__SOURCE: status=success, rows_affected=1" in output

def test_row_summary_reports_rows_affected_for_noop(tmp_path: Path, capsys) -> None:
    project_dir = tmp_path / "generated"
    target_dir = project_dir / "target"
    target_dir.mkdir(parents=True)
    (target_dir / "manifest.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "model.generated.customer": {
                        "resource_type": "model",
                        "name": "customer",
                        "alias": "CUSTOMER",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    (target_dir / "run_results.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "unique_id": "model.generated.customer",
                        "relation_name": "DB.CORE.CUSTOMER",
                        "status": "success",
                        "adapter_response": {
                            "rows_affected": 0,
                            "query_id": "noop-qid",
                        },
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = cli.main(["row-summary", "--project-dir", str(project_dir)])

    output = capsys.readouterr().out
    assert result == 0
    assert "rows_affected=0" in output
