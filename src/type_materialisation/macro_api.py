from abc import ABC, abstractmethod
from typing import Any


class PythonExecutionNotSupported(NotImplementedError):
    """Raised by macros that intentionally do not support local Python execution."""


class TypeMaterialisationMacro(ABC):
    supports_python_execution = False

    @abstractmethod
    def generate_dbt_macro(self) -> str:
        """Return a complete dbt/Jinja macro definition."""

    def execute(self, *, value: Any, **_context: Any) -> Any:
        raise PythonExecutionNotSupported
