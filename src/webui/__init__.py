"""WebUI Control AI Dashboard Server and Bridge module."""
from __future__ import annotations

from typing import Any

__all__ = [
    "DashboardServer",
    "DashboardHandler",
    "HubStore",
    "RegistryStore",
    "CLIS",
    "ASSET_KINDS",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from src.webui import dashboard_server

        return getattr(dashboard_server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

