import importlib.util
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .macro_api import PythonExecutionNotSupported
from .schema import REPO_ROOT


class MacroLoadError(ValueError):
    pass


@dataclass(frozen=True)
class MacroReference:
    module: Any
    module_path: Path
    macro_name: str
    macro: Any


class PythonMacroResolver:
    def __init__(self, *, spec_path: Path, macro_paths: list[Path] | None = None) -> None:
        roots = list(macro_paths or [])
        roots.extend(
            [
                spec_path.parent / "macros",
                Path.cwd() / "macros",
                REPO_ROOT / "macros",
            ]
        )
        self.roots = _dedupe_paths(roots)
        self._modules: dict[Path, Any] = {}

    def call(self, macro_name: str, **context: Any) -> Any:
        macro = self.resolve_python_macro(macro_name)
        if macro is None:
            raise MacroLoadError(f"custom macro `{macro_name}` has no Python execution callable")
        execute = macro.execute
        signature = inspect.signature(execute)
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
            return execute(**context)
        kwargs = {
            name: value
            for name, value in context.items()
            if name in signature.parameters
        }
        try:
            return execute(**kwargs)
        except PythonExecutionNotSupported as exc:
            raise MacroLoadError(f"custom macro `{macro_name}` has no Python execution callable") from exc

    def has_python_callable(self, macro_name: str) -> bool:
        return self.resolve_python_macro(macro_name) is not None

    def require_sql_generation(self, macro_name: str) -> None:
        reference = self.resolve_reference(macro_name)
        generator = getattr(reference.macro, "generate_dbt_macro", None)
        if not callable(generator):
            raise MacroLoadError(
                f"custom macro `{macro_name}` must expose callable `generate_dbt_macro`"
            )
        macro_sql = generator()
        if not isinstance(macro_sql, str) or not macro_sql.strip():
            raise MacroLoadError("`generate_dbt_macro` must return non-empty SQL")
        expected_macro = f"macro {reference.macro_name}("
        if expected_macro not in macro_sql:
            raise MacroLoadError(
                f"generated SQL for `{reference.macro_name}` must define a dbt macro "
                f"named `{reference.macro_name}`"
            )

    def resolve_python_macro(self, macro_name: str) -> Any | None:
        reference = self.resolve_reference(macro_name)
        execute = getattr(reference.macro, "execute", None)
        if not getattr(reference.macro, "supports_python_execution", False):
            return None
        if callable(execute):
            return reference.macro
        raise MacroLoadError(f"`{macro_name}` declares Python execution but has no callable `execute` method")

    def resolve_reference(self, macro_name: str) -> MacroReference:
        module_name, separator, object_name = macro_name.rpartition(".")
        if not separator:
            raise MacroLoadError("custom macro must be a dotted Python reference such as module.macro")
        for root in self.roots:
            module_path = root / f"{module_name.replace('.', '/')}.py"
            if module_path.exists():
                module = self._load_module(module_path)
                macro = getattr(module, object_name, None)
                if macro is None:
                    raise MacroLoadError(f"`{object_name}` is not defined in `{module_path}`")
                return MacroReference(
                    module=module,
                    module_path=module_path,
                    macro_name=object_name,
                    macro=macro,
                )
        searched = ", ".join(str(path) for path in self.roots)
        raise MacroLoadError(f"custom macro `{macro_name}` was not found; searched {searched}")

    def _load_module(self, module_path: Path) -> Any:
        resolved = module_path.resolve()
        if resolved in self._modules:
            return self._modules[resolved]
        spec = importlib.util.spec_from_file_location(
            f"tms_custom_macro_{abs(hash(resolved))}",
            resolved,
        )
        if spec is None or spec.loader is None:
            raise MacroLoadError(f"could not load Python macro module `{module_path}`")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._modules[resolved] = module
        return module


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    deduped: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            deduped.append(resolved)
    return deduped
