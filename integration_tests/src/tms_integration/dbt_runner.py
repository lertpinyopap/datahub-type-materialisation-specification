from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class TmsCommandError(RuntimeError):
    """Raised when a tms command exits unsuccessfully."""


@dataclass(frozen=True)
class TmsResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


def run_tms_dbt_project(
    *,
    spec_path: Path,
    project_dir: Path,
    target_schema: str,
    dbt_vars: dict[str, Any] | None = None,
) -> TmsResult:
    executable = _tms_executable()
    merged_vars = {"target_schema": target_schema, "tms_job_schema": target_schema}
    if dbt_vars:
        merged_vars.update(dbt_vars)
    command = [
        executable,
        "dbt-build",
        "--spec",
        str(spec_path),
        "--project-dir",
        str(project_dir),
        "--target",
        os.environ.get("TMS_INTEGRATION_DBT_TARGET", "dev"),
        "--vars",
        json.dumps(merged_vars),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    result = TmsResult(args=command, returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
    if completed.returncode != 0:
        raise TmsCommandError(
            "\n".join(
                [
                    f"tms command failed: {' '.join(command)}",
                    completed.stdout,
                    completed.stderr,
                ]
            )
        )
    return result


def _tms_executable() -> str:
    executable = shutil.which("tms")
    if executable is None:
        sibling = Path(sys.executable).with_name("tms")
        if sibling.exists():
            return str(sibling)
        raise TmsCommandError("tms executable was not found on PATH")
    return executable
