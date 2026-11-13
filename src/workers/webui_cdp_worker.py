"""WebUI CDP worker adapter wrapping browser_cdp_worker.cjs with automatic packet fallback."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.workers.base import (
    WorkerAdapter,
    WorkerResult,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    STATUS_CANCELLED,
    STATUS_AWAITING_WEBUI_PACKET,
)
from src.workers.webui_packet_worker import (
    PROVIDERS,
    DEFAULT_ROLES,
    DEFAULT_TASKS,
    make_packet,
    prompt_for,
    verify_and_import_response,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CDP_SCRIPT_PATH = REPO_ROOT / "src" / "workers" / "browser_cdp_worker.cjs"
DEFAULT_MANIFEST = REPO_ROOT / "config" / "browser_targets.example.json"


def probe_webui_targets(manifest_path: str | Path, cdp_port: int = 9222) -> Dict[str, Any]:
    """Probe whether the local browser CDP port is open and identify matching WebUI tabs."""
    endpoint = f"http://127.0.0.1:{cdp_port}/json/list"
    try:
        req = urllib.request.Request(endpoint)
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            tabs = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {
            "cdp_available": False,
            "port": cdp_port,
            "error": f"Cannot connect to CDP on 127.0.0.1:{cdp_port}: {exc}",
            "open_tabs": [],
            "matched_providers": [],
        }

    m_path = Path(manifest_path)
    if not m_path.is_file():
        m_path = DEFAULT_MANIFEST

    manifest_providers = {}
    if m_path.is_file():
        try:
            m_data = json.loads(m_path.read_text(encoding="utf-8"))
            manifest_providers = m_data.get("providers", {}) or m_data.get("targets", {})
        except Exception:
            pass

    page_tabs = [t for t in tabs if t.get("type") == "page"]
    matched_providers = []

    for name, pcfg in manifest_providers.items():
        expected_url = pcfg.get("url", "")
        for t in page_tabs:
            tab_url = t.get("url", "")
            if expected_url and (tab_url == expected_url or tab_url.startswith(expected_url.rstrip("/"))):
                matched_providers.append({
                    "provider": name,
                    "target_url": expected_url,
                    "tab_title": t.get("title", ""),
                    "tab_url": tab_url,
                    "webSocketDebuggerUrl": t.get("webSocketDebuggerUrl"),
                })

    return {
        "cdp_available": True,
        "port": cdp_port,
        "total_page_tabs": len(page_tabs),
        "matched_providers": matched_providers,
        "error": None,
    }


class WebUICDPWorker(WorkerAdapter):
    """CDP-powered browser worker with graceful fallback to copy-paste packet workflow."""

    def __init__(
        self,
        cdp_port: int = 9222,
        manifest_path: Optional[Path] = None,
        approve_send: bool = False,
    ):
        self.cdp_port = cdp_port
        self.manifest_path = manifest_path or (
            REPO_ROOT / "config" / "browser_targets.json"
            if (REPO_ROOT / "config" / "browser_targets.json").is_file()
            else DEFAULT_MANIFEST
        )
        self.approve_send = approve_send

    def capabilities(self) -> Dict[str, Any]:
        probe = probe_webui_targets(self.manifest_path, self.cdp_port)
        return {
            "worker_type": "webui_cdp",
            "cdp_port": self.cdp_port,
            "manifest_path": str(self.manifest_path),
            "approve_send": self.approve_send,
            "probe": probe,
            "supported_providers": list(PROVIDERS),
            "fallback_mode": "dashboard_copy_paste",
        }

    def cancel(self, attempt_id: str) -> bool:
        return True

    def start(
        self,
        job_contract: Any,
        sandbox_dir: str,
        attempt_id: str,
        **kwargs: Any,
    ) -> WorkerResult:
        start_time = time.time()
        sandbox = Path(sandbox_dir).resolve()
        sandbox.mkdir(parents=True, exist_ok=True)

        if hasattr(job_contract, "to_dict"):
            c_dict = job_contract.to_dict()
        elif isinstance(job_contract, dict):
            c_dict = job_contract
        else:
            c_dict = vars(job_contract) if hasattr(job_contract, "__dict__") else {}

        job_id = str(c_dict.get("job_id", kwargs.get("job_id", "webui-job-001")))
        case_id = str(c_dict.get("plan_id", kwargs.get("case_id", "WEBUI-CASE-001")))
        scenario = str(c_dict.get("goal", kwargs.get("scenario", "Conduct independent analysis.")))
        context_id = kwargs.get("context_id", "B")
        role = kwargs.get("role", DEFAULT_ROLES.get(context_id, "Analyst"))
        provider = kwargs.get("provider", "chatgpt").lower()
        approve = kwargs.get("approve_send", self.approve_send)
        cdp_port = int(kwargs.get("cdp_port", self.cdp_port))
        deadline_sec = int(c_dict.get("timeout_seconds", kwargs.get("deadline_seconds", 60)))
        fixture = bool(kwargs.get("fixture", False))

        packet = make_packet(
            job_id=job_id,
            case_id=case_id,
            context_id=context_id,
            role=role,
            provider=provider,
            scenario=scenario,
            task=c_dict.get("task", DEFAULT_TASKS.get(context_id, "Review scenario")),
            ttl_seconds=max(deadline_sec, 60),
        )
        prompt_text = prompt_for(packet)

        # Write prompt and packet to sandbox
        prompt_file = sandbox / f"prompt_{job_id}_{context_id}.txt"
        packet_file = sandbox / f"packet_{job_id}_{context_id}.json"
        prompt_file.write_text(prompt_text, encoding="utf-8")
        packet_file.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")

        # Check if Node and CDP are available
        node_bin = shutil.which("node")
        probe = probe_webui_targets(self.manifest_path, cdp_port)

        can_attempt_cdp = (
            approve
            and node_bin
            and CDP_SCRIPT_PATH.is_file()
            and self.manifest_path.is_file()
            and (probe.get("cdp_available") or fixture)
        )

        if not can_attempt_cdp:
            # Automatic fallback to manual packet workflow
            duration_ms = int((time.time() - start_time) * 1000)
            reason = "CDP not approved" if not approve else (
                "Node.js not installed" if not node_bin else (
                    f"CDP port {cdp_port} unavailable" if not probe.get("cdp_available") else "Target tab not found"
                )
            )
            return WorkerResult(
                job_id=job_id,
                attempt_id=attempt_id,
                worker_type="webui_cdp",
                status=STATUS_AWAITING_WEBUI_PACKET,
                changed_files=[packet_file.name, prompt_file.name],
                stdout=prompt_text,
                exit_code=0,
                duration_ms=duration_ms,
                metadata={
                    "fallback": "dashboard_copy_paste",
                    "reason": reason,
                    "probe": probe,
                    "packet": packet,
                },
            )

        # Execute Node CDP script
        cmd = [
            node_bin,
            str(CDP_SCRIPT_PATH),
            "--approve-send",
            "--role", context_id,
            "--provider", provider,
            "--manifest", str(self.manifest_path),
            "--cdp-port", str(cdp_port),
            "--deadline-seconds", str(deadline_sec),
            "--prompt-file", str(prompt_file),
            "--job", job_id,
        ]
        if fixture:
            cmd.append("--fixture")

        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=deadline_sec + 10,
            )
            duration_ms = int((time.time() - start_time) * 1000)

            if res.returncode == 0:
                raw_out = res.stdout.strip()
                parsed_out = json.loads(raw_out) if raw_out else {}
                captured_reply = parsed_out.get("reply", {})
                validation = verify_and_import_response(packet, captured_reply)
                if validation.get("valid"):
                    return WorkerResult(
                        job_id=job_id,
                        attempt_id=attempt_id,
                        worker_type="webui_cdp",
                        status=STATUS_COMPLETED,
                        changed_files=[packet_file.name, prompt_file.name],
                        stdout=json.dumps(captured_reply, ensure_ascii=False),
                        exit_code=0,
                        duration_ms=duration_ms,
                        metadata={"cdp_result": parsed_out, "validation": validation},
                    )

            # In case of CDP failure (e.g., bot check, selector changed, timeout)
            # automatically fallback to copy-paste packet workflow without failing the entire orchestra pipeline!
            return WorkerResult(
                job_id=job_id,
                attempt_id=attempt_id,
                worker_type="webui_cdp",
                status=STATUS_AWAITING_WEBUI_PACKET,
                changed_files=[packet_file.name, prompt_file.name],
                stdout=prompt_text,
                stderr=res.stderr or "CDP bridge returned non-zero code.",
                exit_code=res.returncode,
                duration_ms=duration_ms,
                metadata={
                    "fallback": "dashboard_copy_paste",
                    "reason": "CDP automation blocked or timed out; operator manual relay active.",
                    "cdp_stderr": res.stderr[:300],
                    "packet": packet,
                },
            )
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            return WorkerResult(
                job_id=job_id,
                attempt_id=attempt_id,
                worker_type="webui_cdp",
                status=STATUS_AWAITING_WEBUI_PACKET,
                changed_files=[packet_file.name, prompt_file.name],
                stdout=prompt_text,
                stderr=f"CDP execution error: {e}",
                exit_code=1,
                duration_ms=duration_ms,
                metadata={
                    "fallback": "dashboard_copy_paste",
                    "reason": f"CDP exception: {e}",
                    "packet": packet,
                },
            )
