"""Base worker adapter interface and result models for WebUI Control AI."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List


STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_CANCELLED = "CANCELLED"
STATUS_BLOCKED_TOOL_UNAVAILABLE = "BLOCKED_TOOL_UNAVAILABLE"
STATUS_AWAITING_WEBUI_PACKET = "AWAITING_WEBUI_PACKET"

VALID_STATUSES = {
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    STATUS_CANCELLED,
    STATUS_BLOCKED_TOOL_UNAVAILABLE,
    STATUS_AWAITING_WEBUI_PACKET,
}


@dataclass
class WorkerResult:
    """Standard execution result returned by all WorkerAdapter implementations."""
    job_id: str
    attempt_id: str
    worker_type: str
    status: str
    changed_files: List[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration_ms: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"Invalid worker status: {self.status}. Must be one of {VALID_STATUSES}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "attempt_id": self.attempt_id,
            "worker_type": self.worker_type,
            "status": self.status,
            "changed_files": list(self.changed_files),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WorkerResult:
        return cls(
            job_id=str(data.get("job_id", "")),
            attempt_id=str(data.get("attempt_id", "")),
            worker_type=str(data.get("worker_type", "unknown")),
            status=str(data.get("status", STATUS_FAILED)),
            changed_files=list(data.get("changed_files", [])),
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
            exit_code=int(data.get("exit_code", 0)),
            duration_ms=int(data.get("duration_ms", 0)),
            metadata=dict(data.get("metadata", {})),
        )


class WorkerAdapter(ABC):
    """Abstract base class for all execution workers (Local CLI, WebUI, Mock)."""

    @abstractmethod
    def start(self, job_contract: Any, sandbox_dir: str, attempt_id: str, **kwargs: Any) -> WorkerResult:
        """Execute or initiate a job contract inside sandbox_dir."""
        pass

    @abstractmethod
    def cancel(self, attempt_id: str) -> bool:
        """Cancel an ongoing job attempt."""
        pass

    @abstractmethod
    def capabilities(self) -> Dict[str, Any]:
        """Return worker capabilities and supported protocols."""
        pass
