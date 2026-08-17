from pathlib import Path
from typing import Any


def csv_load_method(source: dict[str, Any]) -> str:
    return str(source.get("load_method", "stage"))


def csv_seed_file_path(spec: dict[str, Any], spec_path: Path) -> Path:
    del spec_path
    seed = spec["source"].get("seed", {})
    if not isinstance(seed, dict):
        seed = {}
    raw_path = seed.get("file")
    if not isinstance(raw_path, str):
        raise OSError("source.seed.file is required when source.load_method is dbt_seed")
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise OSError(f"seed CSV file does not exist: {path}")
    return path
