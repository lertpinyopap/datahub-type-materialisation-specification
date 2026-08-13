import argparse
import sys
import tempfile
from pathlib import Path

from .csv_validate import validate_csv_file
from .dbt_generate import GenerateDbtOptions, generate_dbt_project
from .errors import DependencyError, Diagnostic
from .schema import load_yaml, validate_schema
from .spec import validate_parse_semantics


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
        "--abstract",
        action="store_true",
        help="validate as an abstract/partial specification",
    )

    validate_parser = subparsers.add_parser("validate", help="validate a CSV file according to a concrete spec")
    validate_parser.add_argument("--spec", required=True, type=Path, help="path to a concrete specification YAML file")
    validate_parser.add_argument("--input-file", required=True, type=Path, help="path to a CSV file")
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
        "--output-dir",
        type=Path,
        help="directory for the generated dbt project; defaults to a fresh temp directory",
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
    return parser


def _parse(args: argparse.Namespace) -> int:
    diagnostics = parse_spec(args.spec, abstract=args.abstract)
    if diagnostics:
        _print_diagnostics("parse failed", diagnostics)
        return 1
    mode = "abstract" if args.abstract else "concrete"
    _print_success("parse passed", f"{args.spec} is a valid {mode} specification")
    return 0


def _validate(args: argparse.Namespace) -> int:
    diagnostics = parse_spec(args.spec, abstract=False)
    if diagnostics:
        _print_diagnostics("spec parse failed", diagnostics)
        return 1
    result = validate_csv_file(args.spec, args.input_file, macro_paths=args.macro_path)
    if result.warnings:
        _print_warnings(result.warnings)
    if result.errors:
        _print_diagnostics("validation failed", result.errors)
        _console().print(f"[dim]checked {result.rows_checked} rows[/dim]")
        return 1
    _print_success(
        "validation passed",
        f"{args.input_file} is valid according to {args.spec} ({result.rows_checked} rows checked)",
    )
    return 0


def _generate_dbt(args: argparse.Namespace) -> int:
    diagnostics = parse_spec(args.spec, abstract=False)
    if diagnostics:
        _print_diagnostics("spec parse failed", diagnostics)
        return 1
    output_dir = args.output_dir or Path(tempfile.mkdtemp(prefix="tms-dbt-"))
    result = generate_dbt_project(
        GenerateDbtOptions(
            spec_path=args.spec,
            output_dir=output_dir,
            csv_stage=args.csv_stage,
            unit_test_csv=args.unit_test_csv,
            macro_paths=args.macro_path,
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


def parse_spec(spec_path: Path, *, abstract: bool) -> list[Diagnostic]:
    if not spec_path.exists():
        return [Diagnostic("spec file does not exist", str(spec_path))]
    data = load_yaml(spec_path)
    if not isinstance(data, dict):
        return [Diagnostic("specification root must be a mapping", "$")]
    diagnostics = validate_schema(data, abstract=abstract)
    diagnostics.extend(validate_parse_semantics(data, abstract=abstract))
    return diagnostics


def _print_diagnostics(title: str, diagnostics: list[Diagnostic]) -> None:
    console = _console(stderr=True)
    table = _diagnostic_table(diagnostics, style="red")
    console.print(f"[bold red]{title}[/bold red]")
    console.print(table)


def _print_warnings(warnings: list[Diagnostic]) -> None:
    console = _console(stderr=True)
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
