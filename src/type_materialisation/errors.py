from dataclasses import dataclass


@dataclass(frozen=True)
class Diagnostic:
    message: str
    location: str | None = None

    def render(self) -> str:
        if self.location:
            return f"{self.location}: {self.message}"
        return self.message


class TmsError(Exception):
    """Base exception for expected CLI failures."""


class DependencyError(TmsError):
    """Raised when an optional runtime dependency is not installed."""
