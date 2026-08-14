import subprocess
from pathlib import Path

from type_materialisation import cli
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
