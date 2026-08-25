import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .csv_validate import validate_csv_file
from .dbt_generate import GenerateDbtOptions, generate_dbt_project
from .errors import DependencyError, Diagnostic
from .inheritance import InheritanceError, resolve_spec
from .schema import validate_schema
from .source_files import csv_load_method, csv_seed_file_path
from .spec import validate_parse_semantics
from .variables import parse_vars, resolve_python_template, resolve_python_templates


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "parse":
            return _parse(args)
        if args.command == "validate":
            return _validate(args)
        if args.command == "generate-dbt":
            return _generate_dbt(args)
        if args.command == "dbt-build":
            return _dbt_build(args)
        if args.command == "row-summary":
            return _row_summary(args)
    except DependencyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command {args.command}")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tms",
        description="Utilities for the Type Materialisation Specification.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse_parser = subparsers.add_parser("parse", help="check a YAML spec against schema and spec rules")
    parse_parser.add_argument("--spec", required=True, type=Path, help="path to a specification YAML file")
    parse_parser.add_argument(
        "--spec-path",
        action="append",
        default=[],
        type=Path,
        help="directory searched for inherited parent specifications; may be provided more than once",
    )
    parse_parser.add_argument(
        "--abstract",
        action="store_true",
        help="validate as an abstract/partial specification",
    )
    parse_parser.add_argument(
        "--vars",
        help="YAML/JSON mapping used to resolve TMS variable expressions before parsing",
    )

    validate_parser = subparsers.add_parser("validate", help="validate a CSV file according to a concrete spec")
    validate_parser.add_argument("--spec", required=True, type=Path, help="path to a concrete specification YAML file")
    validate_parser.add_argument(
        "--spec-path",
        action="append",
        default=[],
        type=Path,
        help="directory searched for inherited parent specifications; may be provided more than once",
    )
    validate_parser.add_argument(
        "--input-file",
        type=Path,
        help="path to a CSV file; defaults to source.seed.file for dbt_seed CSV sources",
    )
    validate_parser.add_argument(
        "--vars",
        help="YAML/JSON mapping used to resolve Python-side variable expressions before validation",
    )
    validate_parser.add_argument(
        "--macro-path",
        action="append",
        default=[],
        type=Path,
        help="path containing Python macro modules; may be provided more than once",
    )

    generate_parser = subparsers.add_parser("generate-dbt", help="generate a dbt project from a concrete spec")
    generate_parser.add_argument("--spec", required=True, type=Path, help="path to a concrete specification YAML file")
    generate_parser.add_argument(
        "--spec-path",
        action="append",
        default=[],
        type=Path,
        help="directory searched for inherited parent specifications; may be provided more than once",
    )
    generate_parser.add_argument(
        "--output-dir",
        type=Path,
        help="directory for the generated dbt project; defaults to ./tmp/<spec file stem>",
    )
    generate_parser.add_argument(
        "--csv-stage",
        help="Snowflake stage name/path override; defaults to source.location in the spec",
    )
    generate_parser.add_argument(
        "--unit-test-csv",
        type=Path,
        help="optional sample CSV used to generate dbt unit-test rows",
    )
    generate_parser.add_argument(
        "--macro-path",
        action="append",
        default=[],
        type=Path,
        help="path containing Python macro modules; may be provided more than once",
    )
    generate_parser.add_argument(
        "--vars",
        help="YAML/JSON mapping used to resolve Python-side variable expressions before generation",
    )

    build_parser = subparsers.add_parser("dbt-build", help="build a generated dbt project for a spec")
    build_parser.add_argument("--spec", required=True, type=Path, help="path to a concrete specification YAML file")
    build_parser.add_argument(
        "--project-dir",
        type=Path,
        help="generated dbt project directory; defaults to ./tmp/<spec file stem>",
    )
    build_parser.add_argument("--target", required=True, help="dbt target name passed through to dbt build")
    build_parser.add_argument("--vars", help="dbt vars YAML/JSON string passed through to dbt build")
    build_parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="pass --full-refresh through to dbt build before printing the TMS row summary",
    )

    row_summary_parser = subparsers.add_parser("row-summary", help="print target row summary for a dbt project")
    row_summary_parser.add_argument(
        "--project-dir",
        required=True,
        type=Path,
        help="generated dbt project directory containing target/run_results.json",
    )
    return parser


def _parse(args: argparse.Namespace) -> int:
    variables, var_diagnostics = parse_vars(args.vars)
    if var_diagnostics:
        _print_diagnostics("vars parse failed", var_diagnostics)
        return 1
    diagnostics = parse_spec(args.spec, abstract=args.abstract, spec_paths=args.spec_path, variables=variables)
    if diagnostics:
        _print_diagnostics("parse failed", diagnostics)
        return 1
    mode = "abstract" if args.abstract else "concrete"
    _print_success("parse passed", f"{args.spec} is a valid {mode} specification")
    return 0


def _validate(args: argparse.Namespace) -> int:
    variables, var_diagnostics = parse_vars(args.vars)
    if var_diagnostics:
        _print_diagnostics("vars parse failed", var_diagnostics)
        return 1
    resolved, diagnostics = resolve_and_validate_spec(
        args.spec,
        abstract=False,
        spec_paths=args.spec_path,
        variables=variables,
    )
    if diagnostics:
        _print_diagnostics("spec parse failed", diagnostics)
        return 1
    input_file, input_diagnostics = _validation_input_file(
        args.input_file,
        resolved,
        args.spec,
        variables,
    )
    if input_diagnostics:
        _print_diagnostics("input file resolution failed", input_diagnostics)
        return 1
    assert input_file is not None
    result = validate_csv_file(
        args.spec,
        input_file,
        macro_paths=args.macro_path,
        spec=resolved,
        spec_paths=args.spec_path,
    )
    if result.warnings:
        _print_warnings(result.warnings)
    if result.errors:
        _print_diagnostics("validation failed", result.errors)
        _console().print(f"[dim]checked {result.rows_checked} rows[/dim]")
        return 1
    _print_success(
        "validation passed",
        f"{input_file} is valid according to {args.spec} ({result.rows_checked} rows checked)",
    )
    return 0


def _validation_input_file(
    input_file: Path | None,
    spec: dict,
    spec_path: Path,
    variables: dict,
) -> tuple[Path | None, list[Diagnostic]]:
    if input_file is not None:
        try:
            return Path(resolve_python_template(str(input_file), variables=variables)), []
        except ValueError as exc:
            return None, [Diagnostic(str(exc), "--input-file")]

    source = spec.get("source", {})
    if (
        isinstance(source, dict)
        and source.get("format") == "csv"
        and csv_load_method(source) == "dbt_seed"
    ):
        try:
            return csv_seed_file_path(spec, spec_path), []
        except OSError as exc:
            return None, [Diagnostic(str(exc), "$.source.seed.file")]

    return None, [Diagnostic("--input-file is required unless source.load_method is dbt_seed", "--input-file")]


def _generate_dbt(args: argparse.Namespace) -> int:
    variables, var_diagnostics = parse_vars(args.vars)
    if var_diagnostics:
        _print_diagnostics("vars parse failed", var_diagnostics)
        return 1
    resolved, diagnostics = resolve_and_validate_spec(
        args.spec,
        abstract=False,
        spec_paths=args.spec_path,
        variables=variables,
    )
    if diagnostics:
        _print_diagnostics("spec parse failed", diagnostics)
        return 1
    output_dir = args.output_dir or _default_dbt_project_dir(args.spec)
    result = generate_dbt_project(
        GenerateDbtOptions(
            spec_path=args.spec,
            output_dir=output_dir,
            csv_stage=args.csv_stage,
            unit_test_csv=args.unit_test_csv,
            macro_paths=args.macro_path,
            spec=resolved,
            spec_paths=args.spec_path,
            vars=variables,
        )
    )
    if result.warnings:
        _print_warnings(result.warnings)
    if result.errors:
        _print_diagnostics("dbt generation failed", result.errors)
        return 1
    _print_success("dbt project generated", str(result.output_dir))
    _console().print("[bold]files[/bold]")
    for path in result.files:
        _console().print(f"  [dim]{path}[/dim]")
    return 0


def _dbt_build(args: argparse.Namespace) -> int:
    if not args.spec.exists():
        _print_diagnostics("dbt build failed", [Diagnostic("spec file does not exist", str(args.spec))])
        return 1
    executable = resolve_dbt_executable()
    project_dir = args.project_dir or _default_dbt_project_dir(args.spec)
    command_args = ["--project-dir", str(project_dir)]
    if args.target:
        command_args.extend(["--target", args.target])
    if args.vars:
        command_args.extend(["--vars", args.vars])
    if args.full_refresh:
        command_args.append("--full-refresh")
    completed = subprocess.run([executable, "build", *command_args], check=False)
    if completed.returncode == 0:
        print_dbt_row_summary(project_dir)
    return completed.returncode


def _row_summary(args: argparse.Namespace) -> int:
    print_dbt_row_summary(args.project_dir)
    return 0


def resolve_dbt_executable() -> str:
    env_value = os.environ.get("DBT_EXECUTABLE_PATH")
    if env_value:
        return env_value

    executable = shutil.which("dbt")
    if executable is None:
        raise DependencyError("dbt executable was not found on PATH.")
    return executable


def print_dbt_row_summary(project_dir: Path) -> None:
    """Print dbt/Snowflake adapter DML metrics for generated target models."""
    results_path = project_dir / "target" / "run_results.json"
    manifest_path = project_dir / "target" / "manifest.json"
    if not results_path.exists():
        print(f"TMS row summary unavailable: {results_path} was not created.")
        return

    try:
        payload = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"TMS row summary unavailable: could not read {results_path}: {exc}")
        return

    target_model_ids = load_dbt_target_model_ids(manifest_path)
    reported_count = 0
    print("TMS dbt write summary:")
    for result in payload.get("results", []):
        if not isinstance(result, dict):
            continue

        unique_id = result.get("unique_id", "")
        if target_model_ids and unique_id not in target_model_ids:
            continue

        adapter_response = result.get("adapter_response") or {}
        rows_affected = adapter_response.get("rows_affected")
        relation_name = result.get("relation_name") or result.get("unique_id", "unknown")
        status = result.get("status", "unknown")
        duration = format_optional_float(result.get("execution_time"))

        reported_count += 1
        print(
            "  "
            f"{relation_name}: "
            f"status={status}, "
            f"rows_affected={format_optional_int(rows_affected)}, "
            f"duration_seconds={duration}, "
            f"query_id={adapter_response.get('query_id', 'unavailable')}"
        )

    if reported_count == 0:
        print("TMS dbt write summary unavailable: dbt returned no target model result.")
    else:
        print(
            "Note: rows_affected is the dbt/Snowflake adapter response and may represent "
            "physical rows processed by the materialization, not business-change counts. "
            "For SCD2, use business hashes and validity columns to validate actual history changes."
        )


def format_optional_int(value: object) -> str:
    if value is None:
        return "unavailable"

    try:
        return str(int(value))
    except (TypeError, ValueError):
        return str(value)


def format_optional_float(value: object) -> str:
    if value is None:
        return "unavailable"

    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def load_dbt_target_model_ids(manifest_path: Path) -> set[str]:
    """Return every dbt model id represented in the generated project."""
    if not manifest_path.exists():
        return set()

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()

    target_model_ids: set[str] = set()
    for unique_id, node in payload.get("nodes", {}).items():
        if not isinstance(node, dict):
            continue

        if node.get("resource_type") != "model":
            continue

        target_model_ids.add(unique_id)

    return target_model_ids


def _default_dbt_project_dir(spec_path: Path) -> Path:
    return Path("tmp") / spec_path.stem


def parse_spec(
    spec_path: Path,
    *,
    abstract: bool,
    spec_paths: list[Path] | None = None,
    variables: dict | None = None,
) -> list[Diagnostic]:
    _, diagnostics = resolve_and_validate_spec(
        spec_path,
        abstract=abstract,
        spec_paths=spec_paths,
        variables=variables,
    )
    return diagnostics


def resolve_and_validate_spec(
    spec_path: Path,
    *,
    abstract: bool,
    spec_paths: list[Path] | None = None,
    variables: dict | None = None,
) -> tuple[dict | None, list[Diagnostic]]:
    if not spec_path.exists():
        return None, [Diagnostic("spec file does not exist", str(spec_path))]
    try:
        resolved = resolve_spec(spec_path, spec_paths=spec_paths)
    except InheritanceError as exc:
        return None, [Diagnostic(str(exc), "inheritance")]
    data, diagnostics = resolve_python_templates(resolved.spec, variables=variables)
    diagnostics.extend(validate_schema(data, abstract=abstract))
    diagnostics.extend(validate_parse_semantics(data, abstract=abstract))
    return data, diagnostics


def _print_diagnostics(title: str, diagnostics: list[Diagnostic]) -> None:
    console = _console(stderr=True)
    table = _diagnostic_table(diagnostics, style="red")
    console.print(f"[bold red]{title}[/bold red]")
    console.print(table)


def _print_warnings(warnings: list[Diagnostic]) -> None:
    console = _console()
    table = _diagnostic_table(warnings, style="yellow")
    console.print("[bold yellow]warnings[/bold yellow]")
    console.print(table)


def _print_success(title: str, message: str) -> None:
    console = _console()
    console.print(f"[bold green]OK[/bold green] [green]{title}[/green]")
    console.print(f"[dim]{message}[/dim]")


def _diagnostic_table(diagnostics: list[Diagnostic], *, style: str):
    rich = _require_rich()
    table = rich.table.Table(
        show_header=True,
        header_style=f"bold {style}",
        box=rich.box.SIMPLE,
    )
    table.add_column("Location", style="cyan", no_wrap=True)
    table.add_column("Message", style=style)
    for diagnostic in diagnostics:
        table.add_row(diagnostic.location or "-", diagnostic.message)
    return table


def _console(*, stderr: bool = False):
    rich = _require_rich()
    return rich.console.Console(stderr=stderr)


def _require_rich():
    try:
        from rich import box
        from rich import console
        from rich import table
    except ModuleNotFoundError as exc:
        raise DependencyError(
            "Rich is required. Install dependencies with `pip install -r requirements.txt`."
        ) from exc
    return argparse.Namespace(box=box, console=console, table=table)


if __name__ == "__main__":
    raise SystemExit(main())
