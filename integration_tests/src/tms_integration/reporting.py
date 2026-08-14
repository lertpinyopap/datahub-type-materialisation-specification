from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType
from typing import TextIO

from rich.console import Console
from rich.panel import Panel

from .scenario import LoadStep, Scenario


class IntegrationReporter:
    def __init__(self, console: Console | None = None, *, enabled: bool | None = None) -> None:
        self.enabled = _progress_enabled() if enabled is None else enabled
        self._current_step: str | None = None
        self._output: TextIO | None = None
        if console is None:
            if self.enabled:
                self._output = _terminal_output()
                self.console = Console(file=self._output, force_terminal=True)
            else:
                self.console = Console(file=sys.stderr, force_terminal=False)
        else:
            self.console = console

    def __enter__(self) -> IntegrationReporter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()

    def close(self) -> None:
        if self._output is not None and self._output is not sys.stderr:
            self._output.close()
            self._output = None

    def scenario_start(self, scenario: Scenario, *, schema: str, prefix: str) -> None:
        self._print("")
        self._print("")
        self._print(
            Panel.fit(
                "\n".join(
                    [
                        f"[bold]{scenario.name}[/bold]",
                        scenario.description,
                        f"[dim]schema={schema} prefix={prefix}[/dim]",
                    ]
                ),
                title="TMS integration",
                border_style="cyan",
            )
        )

    def step(self, label: str, detail: str | None = None) -> None:
        self._current_step = label if detail is None else f"{label}: {detail}"
        message = f"[bold cyan]->[/bold cyan] {label}"
        if detail:
            message = f"{message} [dim]{detail}[/dim]"
        self._print(message)

    def ok(self, label: str, detail: str | None = None) -> None:
        message = f"[bold green]ok[/bold green] {label}"
        if detail:
            message = f"{message} [dim]{detail}[/dim]"
        self._print(message)

    def load_start(self, step: LoadStep) -> None:
        self.step(f"Running load {step.name}", str(step.source_csv.name))

    def checks(self, checks: list[str]) -> None:
        if not checks:
            self.step("Checking expected target rows")
            return
        self.step("Checking expected target rows")
        for check in checks:
            self._print(f"   [green]-[/green] {check}")

    def scenario_passed(self, scenario: Scenario) -> None:
        self.ok("Scenario passed", scenario.name)

    def scenario_failed(self, scenario: Scenario, exc: BaseException) -> None:
        details = [
            f"[bold]{scenario.name}[/bold]",
            f"[dim]last step: {self._current_step or 'not started'}[/dim]",
            f"{type(exc).__name__}: {_exception_message(exc)}",
            "[dim]pytest traceback follows below[/dim]",
        ]
        self._print(
            Panel.fit(
                "\n".join(details),
                title="TMS integration failed",
                border_style="red",
            )
        )

    def _print(self, renderable: object) -> None:
        if self.enabled:
            self.console.print(renderable)


def _progress_enabled() -> bool:
    return os.environ.get("TMS_INTEGRATION_PROGRESS", "1") not in {"0", "false", "False", "no", "NO"}


def _terminal_output() -> TextIO:
    try:
        return Path("/dev/tty").open("w", encoding="utf-8", buffering=1)
    except OSError:
        return sys.stderr


def _exception_message(exc: BaseException) -> str:
    message = str(exc).strip()
    if not message:
        return "no exception message"
    first_line = message.splitlines()[0].strip()
    return first_line or "no exception message"
