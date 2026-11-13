"""Workers module for WebUI Control AI."""
from __future__ import annotations

from src.workers.base import (
    STATUS_AWAITING_WEBUI_PACKET,
    STATUS_BLOCKED_TOOL_UNAVAILABLE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    VALID_STATUSES,
    WorkerAdapter,
    WorkerResult,
)
from src.workers.cli_harness_worker import (
    CLIS,
    CLIHarnessWorker,
    DEFAULT_HARNESSES,
    load_harness_config,
    probe_harness,
)
from src.workers.mock_worker import MockWorker
from src.workers.webui_cdp_worker import WebUICDPWorker, probe_webui_targets
from src.workers.webui_packet_worker import (
    DEFAULT_ROLES,
    DEFAULT_TASKS,
    PROTOCOL,
    PROVIDERS,
    WebUIPacketWorker,
    compute_digest,
    make_bundle,
    make_packet,
    prompt_for,
    verify_and_import_response,
)

__all__ = [
    "WorkerResult",
    "WorkerAdapter",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_TIMEOUT",
    "STATUS_CANCELLED",
    "STATUS_BLOCKED_TOOL_UNAVAILABLE",
    "STATUS_AWAITING_WEBUI_PACKET",
    "VALID_STATUSES",
    "MockWorker",
    "CLIHarnessWorker",
    "probe_harness",
    "load_harness_config",
    "DEFAULT_HARNESSES",
    "WebUIPacketWorker",
    "WebUICDPWorker",
    "probe_webui_targets",
    "make_packet",
    "make_bundle",
    "prompt_for",
    "verify_and_import_response",
    "compute_digest",
    "PROTOCOL",
    "PROVIDERS",
    "DEFAULT_ROLES",
    "DEFAULT_TASKS",
]
