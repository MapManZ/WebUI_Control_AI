"""Universal MCP Server package for WebUI Control AI."""
from __future__ import annotations

from typing import Any

__all__ = ["OrchestraMCPServer", "MCP_TOOLS"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from src.mcp import orchestra_mcp

        return getattr(orchestra_mcp, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

