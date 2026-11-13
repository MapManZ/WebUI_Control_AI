"""CLI tools and preflight diagnostics."""

from typing import Any


def run_doctor(*args: Any, **kwargs: Any) -> Any:
    """Lazy loader for doctor preflight check."""
    from src.cli.doctor import run_doctor as _run_doctor
    return _run_doctor(*args, **kwargs)


__all__ = ["run_doctor"]
