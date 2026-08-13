from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import load_yaml
from .spec import case_key


class InheritanceError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedSpec:
    spec: dict[str, Any]
    path: Path
    chain: tuple[Path, ...]


def resolve_spec(spec_path: Path, *, spec_paths: list[Path] | None = None) -> ResolvedSpec:
    return _resolve_spec(spec_path.resolve(), tuple(path.resolve() for path in spec_paths or []), ())


def _resolve_spec(spec_path: Path, spec_paths: tuple[Path, ...], stack: tuple[Path, ...]) -> ResolvedSpec:
    if spec_path in stack:
        cycle = " -> ".join(path.name for path in (*stack, spec_path))
        raise InheritanceError(f"inheritance cycle detected: {cycle}")
    if not spec_path.exists():
        raise InheritanceError(f"spec file does not exist: {spec_path}")

    data = load_yaml(spec_path)
    if not isinstance(data, dict):
        raise InheritanceError(f"specification root must be a mapping: {spec_path}")

    parent_id = data.get("extends")
    if not isinstance(parent_id, str):
        resolved = deepcopy(data)
        resolved.pop("extends", None)
        return ResolvedSpec(spec=resolved, path=spec_path, chain=(spec_path,))

    parent_path = _find_parent(parent_id, spec_path.parent, spec_paths)
    parent = _resolve_spec(parent_path, spec_paths, (*stack, spec_path))
    parent_spec_id = parent.spec.get("id")
    if not isinstance(parent_spec_id, str) or case_key(parent_spec_id) != case_key(parent_id):
        raise InheritanceError(
            f"parent `{parent_id}` resolved to `{parent_path}` but its id is `{parent_spec_id}`"
        )

    child = deepcopy(data)
    child.pop("extends", None)
    return ResolvedSpec(
        spec=_overlay(parent.spec, child),
        path=spec_path,
        chain=(*parent.chain, spec_path),
    )


def _find_parent(parent_id: str, child_dir: Path, spec_paths: tuple[Path, ...]) -> Path:
    search_dirs = _dedupe_dirs((child_dir, *spec_paths))
    for directory in search_dirs:
        candidates = _parent_candidates(parent_id, directory)
        if len(candidates) > 1:
            joined = ", ".join(str(candidate) for candidate in candidates)
            raise InheritanceError(
                f"parent specification `{parent_id}` is ambiguous in {directory}; candidates: {joined}"
            )
        if candidates:
            return candidates[0].resolve()
    searched = ", ".join(str(directory) for directory in search_dirs)
    raise InheritanceError(f"parent specification `{parent_id}` not found; searched {searched}")


def _parent_candidates(parent_id: str, directory: Path) -> list[Path]:
    candidates: list[Path] = []
    for suffix in (".yaml", ".yml"):
        direct = directory / f"{parent_id}{suffix}"
        if direct.exists():
            _append_candidate(candidates, direct)
    for pattern in ("*.yaml", "*.yml"):
        for candidate in sorted(directory.glob(pattern)):
            if case_key(candidate.stem) == case_key(parent_id):
                _append_candidate(candidates, candidate)
    return sorted(candidates)


def _append_candidate(candidates: list[Path], candidate: Path) -> None:
    candidate_key = _file_identity(candidate)
    for existing in candidates:
        if _file_identity(existing) == candidate_key:
            return
    candidates.append(candidate)


def _file_identity(path: Path) -> tuple[int, int] | tuple[str]:
    try:
        stat = path.stat()
    except OSError:
        return (str(path.resolve()),)
    return (stat.st_dev, stat.st_ino)


def _dedupe_dirs(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    seen: set[Path] = set()
    deduped: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(resolved)
    return tuple(deduped)


def _overlay(parent: Any, child: Any, path: tuple[str, ...] = ()) -> Any:
    if _is_field_list_path(path) and isinstance(parent, list) and isinstance(child, list):
        return _overlay_fields(parent, child, path)
    if isinstance(parent, dict) and isinstance(child, dict):
        merged = deepcopy(parent)
        for key, child_value in child.items():
            if key in merged:
                merged[key] = _overlay(merged[key], child_value, (*path, key))
            else:
                merged[key] = deepcopy(child_value)
        return merged
    return deepcopy(child)


def _overlay_fields(parent_fields: list[Any], child_fields: list[Any], path: tuple[str, ...]) -> list[Any]:
    merged = deepcopy(parent_fields)
    positions: dict[str, int] = {}
    for index, field in enumerate(merged):
        if isinstance(field, dict) and isinstance(field.get("id"), str):
            positions[case_key(field["id"])] = index

    for child_field in child_fields:
        if not isinstance(child_field, dict) or not isinstance(child_field.get("id"), str):
            merged.append(deepcopy(child_field))
            continue
        key = case_key(child_field["id"])
        if key in positions:
            index = positions[key]
            merged[index] = _overlay(merged[index], child_field, (*path, key))
        else:
            positions[key] = len(merged)
            merged.append(deepcopy(child_field))
    return merged


def _is_field_list_path(path: tuple[str, ...]) -> bool:
    return len(path) >= 2 and path[-2:] == ("target", "fields")
