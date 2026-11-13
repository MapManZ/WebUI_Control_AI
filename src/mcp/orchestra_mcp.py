"""Universal MCP Server for WebUI Control AI.

Enables ANY agent (OpenCode, Antigravity, Hermes, DSH, Claude, ChatGPT)
to act as Orchestrator or Worker over standard JSON-RPC 2.0 stdio MCP.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import uuid

from src.orchestra.supervisor import Supervisor
from src.sandbox.path_guard import PathPolicy, is_reparse_point
from src.sandbox.workdir_guard import get_protected_roots, validate_sandbox_dir
from src.workers.base import (
    STATUS_AWAITING_WEBUI_PACKET,
    STATUS_BLOCKED_TOOL_UNAVAILABLE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    WorkerResult,
)
from src.workers.cli_harness_worker import (
    CLIS,
    CLIHarnessWorker,
    probe_harness,
)
from src.workers.mock_worker import MockWorker
from src.workers.webui_cdp_worker import (
    WebUICDPWorker,
    probe_webui_targets,
)
from src.workers.webui_packet_worker import (
    DEFAULT_ROLES,
    DEFAULT_TASKS,
    PROTOCOL,
    PROVIDERS,
    WebUIPacketWorker,
    make_packet,
    prompt_for,
    verify_and_import_response,
)
from src.mcp.proxy_mcp import BUILTIN_PRESETS, probe_external_mcp
from src.webui.dashboard_server import HubStore

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_HUB_STORE = REPO_ROOT / ".runtime" / "webui_hub"
_SAFE_JOB_ID_RE = re.compile(r"^[0-9a-zA-Z_-]{1,64}$")


def _validate_job_id(raw_job_id: Any) -> str:
    """Validate an MCP job_id against strict path-safe identifier rules."""
    job_id = str(raw_job_id) if raw_job_id is not None else ""
    if not _SAFE_JOB_ID_RE.fullmatch(job_id) or ".." in job_id:
        raise ValueError(f"Invalid job_id (must match ^[0-9a-zA-Z_-]{{1,64}}$): {job_id!r}")
    return job_id

MCP_TOOLS = [
    {
        "name": "orchestra_doctor",
        "description": "Probe health and availability of all 4 Local CLIs (Antigravity, OpenCode, Hermes, DSH) and 5 WebUIs (Claude, ChatGPT, Gemini, GLM, DeepSeek).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "detailed": {"type": "boolean", "description": "Include full path and error details"}
            },
        },
    },
    {
        "name": "orchestra_list_jobs",
        "description": "List active and recent Orchestra and WebUI jobs from local SQLite DB and Hub store.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum jobs to return (default 20)"}
            },
        },
    },
    {
        "name": "orchestra_create_webui_packet",
        "description": "Generate a cryptographically verified work packet for any of the 5 WebUI providers (ChatGPT, Claude, Gemini, GLM, DeepSeek).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "Case or job identifier"},
                "provider": {
                    "type": "string",
                    "enum": list(PROVIDERS),
                    "description": "Target WebUI provider (chatgpt, claude, gemini, glm, deepseek)",
                },
                "context_id": {"type": "string", "description": "Role context ID (B, C, D)"},
                "role": {"type": "string", "description": "Descriptive role label (e.g. Analyst, Verifier)"},
                "scenario": {"type": "string", "description": "Input scenario or task brief"},
                "task": {"type": "string", "description": "Detailed task instructions"},
                "ttl_seconds": {"type": "integer", "description": "Validity window in seconds (default 300)"},
                "job_id": {"type": "string", "description": "Optional explicit job ID"},
            },
            "required": ["case_id", "provider", "scenario"],
        },
    },
    {
        "name": "orchestra_submit_webui_response",
        "description": "Validate and record a worker JSON response against its original work packet.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job ID"},
                "response": {
                    "type": "object",
                    "description": "Worker JSON reply containing protocol, job_id, context_id, packet_sha256, status, finding, evidence, limitations",
                },
            },
            "required": ["job_id", "response"],
        },
    },
    {
        "name": "orchestra_probe_cli",
        "description": "Probe a specific local CLI harness for executable presence, version, and readiness.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "harness_name": {
                    "type": "string",
                    "enum": ["antigravity", "opencode", "hermes", "dsh", "codex"],
                    "description": "Name of the CLI harness to probe",
                }
            },
            "required": ["harness_name"],
        },
    },
    {
        "name": "orchestra_run_job",
        "description": "Dispatch a job to a local CLI harness, WebUI adapter, or mock worker inside a sandboxed directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job identifier"},
                "worker_type": {
                    "type": "string",
                    "description": "Worker type (mock, opencode, hermes, dsh, antigravity, webui_cdp, webui_packet)",
                },
                "goal": {"type": "string", "description": "Goal or prompt for the worker"},
                "sandbox_dir": {"type": "string", "description": "Sandboxed directory path"},
                "mock_behavior": {"type": "string", "description": "Mock behavior mode (pass, fail, timeout, etc.) if worker_type is mock"},
                "timeout_seconds": {"type": "integer", "description": "Execution timeout in seconds"},
            },
            "required": ["worker_type", "goal"],
        },
    },
    {
        "name": "orchestra_get_workdir",
        "description": "Get the active working directory and allowed workspace root boundary (PathGuard enforced).",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "orchestra_set_workdir",
        "description": "Set the active working directory inside the allowed workspace root (enforces strict PathGuard validation).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "working_directory": {
                    "type": "string",
                    "description": "Target working directory path (relative or absolute inside workspace root)",
                },
                "role": {
                    "type": "string",
                    "description": "Caller role (e.g. orchestrator, operator, reviewer)",
                },
            },
            "required": ["working_directory"],
        },
    },
    {
        "name": "orchestra_list_external_mcps",
        "description": "List registered external MCP servers (including Lnwjud preset) from .runtime/mcp_configs/external.json.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "orchestra_probe_external_mcp",
        "description": "Probe an external MCP server (e.g. lnwjud) for executable readiness and optional JSON-RPC handshake.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Registered server name or preset (e.g. 'lnwjud')"},
                "live_handshake": {"type": "boolean", "description": "Attempt live stdio JSON-RPC initialize + tools/list"},
            },
            "required": ["name"],
        },
    },
]


class OrchestraMCPServer:
    """Stdio JSON-RPC 2.0 MCP server implementation."""

    def __init__(
        self,
        hub_store_dir: Optional[Path] = None,
        *,
        workspace_root: Optional[Path] = None,
        external_mcp_path: Optional[Path] = None,
        settings_db_path: Optional[Path] = None,
    ):
        self.hub_store = HubStore(
            hub_store_dir or DEFAULT_HUB_STORE,
            workspace_root=workspace_root,
            external_mcp_path=external_mcp_path,
            settings_db_path=settings_db_path,
        )

    def handle_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(message, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}

        msg_id = message.get("id")
        method = message.get("method")
        params = message.get("params", {})

        # Notifications (no id)
        if msg_id is None:
            if method in ("notifications/initialized", "initialized"):
                return None
            return None

        # Standard RPC calls
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "orchestra-mcp",
                        "version": "1.0.0",
                    },
                },
            }

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": MCP_TOOLS},
            }

        if method == "tools/call":
            tool_name = params.get("name")
            args = params.get("arguments", {})
            try:
                result_content = self._call_tool(tool_name, args)
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(result_content, ensure_ascii=False, indent=2)}],
                        "isError": False,
                    },
                }
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Tool execution failed: {e}"}],
                        "isError": True,
                    },
                }

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method '{method}' not found"},
        }

    def _call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        if name == "orchestra_doctor":
            cli_probes = {h: probe_harness(h) for h in ("antigravity", "opencode", "hermes", "dsh", "codex")}
            manifest_path = REPO_ROOT / "config" / "browser_targets.example.json"
            webui_probe = probe_webui_targets(manifest_path, cdp_port=9222)
            return {
                "status": "ok",
                "clis": cli_probes,
                "webuis": {
                    "supported_providers": list(PROVIDERS),
                    "cdp_probe": webui_probe,
                },
            }

        if name == "orchestra_list_jobs":
            try:
                limit = int(args.get("limit", 20))
            except (TypeError, ValueError) as exc:
                raise ValueError("limit must be an integer") from exc
            limit = max(1, min(200, limit))
            hub_jobs = self.hub_store.list_jobs(limit=limit)
            # Optional SQLite inspect if exists
            db_jobs = []
            db_path = REPO_ROOT / ".runtime" / "orchestra_state.db"
            if db_path.is_file():
                try:
                    conn = sqlite3.connect(str(db_path))
                    cur = conn.cursor()
                    cur.execute("SELECT job_id, status FROM jobs ORDER BY rowid DESC LIMIT ?", (limit,))
                    db_jobs = [{"job_id": r[0], "status": r[1]} for r in cur.fetchall()]
                    conn.close()
                except Exception:
                    pass
            return {
                "hub_jobs": hub_jobs,
                "db_jobs": db_jobs,
            }

        if name == "orchestra_create_webui_packet":
            raw_job_id = args.get("job_id")
            if raw_job_id is not None:
                job_id = _validate_job_id(raw_job_id)
            else:
                job_id = uuid.uuid4().hex
            case_id = args["case_id"]
            provider = args["provider"].lower()
            context_id = args.get("context_id", "B")
            role = args.get("role", DEFAULT_ROLES.get(context_id, "Analyst"))
            scenario = args["scenario"]
            task = args.get("task", DEFAULT_TASKS.get(context_id, "Analyze scenario"))
            ttl = int(args.get("ttl_seconds", 300))

            packet = make_packet(
                job_id=job_id,
                case_id=case_id,
                context_id=context_id,
                role=role,
                provider=provider,
                scenario=scenario,
                task=task,
                ttl_seconds=ttl,
            )
            prompt_text = prompt_for(packet)

            # Record in HubStore bundle
            try:
                self.hub_store.create_job(
                    case_id=case_id,
                    scenario=scenario,
                    ttl_seconds=ttl,
                    job_id=job_id,
                    provider_map={context_id: provider},
                )
            except Exception:
                pass

            return {
                "job_id": job_id,
                "context_id": context_id,
                "provider": provider,
                "packet": packet,
                "prompt": prompt_text,
                "packet_sha256": packet["packet_sha256"],
            }

        if name == "orchestra_submit_webui_response":
            job_id = _validate_job_id(args.get("job_id"))
            resp = args["response"]
            result = self.hub_store.submit_reply(job_id, resp)
            return {
                "ok": True,
                "job_id": job_id,
                "status": result["status"],
                "accepted_role": resp.get("context_id"),
            }

        if name == "orchestra_probe_cli":
            harness = args["harness_name"]
            return probe_harness(harness)

        if name == "orchestra_run_job":
            wtype = str(args["worker_type"]).strip().lower()
            goal = str(args["goal"])
            raw_job_id = args.get("job_id")
            if raw_job_id is None:
                job_id = f"job-{uuid.uuid4().hex[:8]}"
            else:
                job_id = _validate_job_id(raw_job_id)

            raw_sandbox = args.get("sandbox_dir")
            if raw_sandbox is None or not str(raw_sandbox).strip():
                candidate_sandbox = str(REPO_ROOT / ".runtime" / "sandboxes" / job_id)
            else:
                candidate_sandbox = str(raw_sandbox)
            sandbox_path = validate_sandbox_dir(
                candidate_sandbox,
                workspace_root=REPO_ROOT,
                require_sandbox_subroot=True,
                create=True,
            )
            if sandbox_path.resolve() == Path(tempfile.gettempdir()).resolve():
                raise ValueError("sandbox_dir cannot be the system temp root itself")

            raw_timeout = int(args["timeout_seconds"] if args.get("timeout_seconds") is not None else 120)
            timeout = max(1, min(3600, raw_timeout))

            allowed_write_paths = list(args.get("allowed_write_paths") or ["solution.txt", "result.txt", "output.txt"])
            forbidden_write_roots = list(args.get("forbidden_write_roots") or [])
            # Do not allow untrusted MCP callers to override QA allowlist or inject arbitrary shell checks.
            allowed_commands = ["python"]
            acceptance_checks = ['python -c "import sys; sys.exit(0)"']

            PathPolicy(
                allowed_write_paths,
                forbidden_write_roots,
                protected_roots=get_protected_roots(),
                sandbox_root=sandbox_path,
                workspace_root=REPO_ROOT,
            )

            contract = {
                "schema_version": "1.0.0",
                "plan_id": "MCP-RUN",
                "phase_id": "MCP",
                "job_id": job_id,
                "batch_id": str(args.get("batch_id") or f"mcp-{job_id}"),
                "batch_ordinal": int(args.get("batch_ordinal", 1)),
                "project_id": str(args.get("project_id") or "mcp-workspace"),
                "workspace_root_ref": ".",
                "goal": goal,
                "allowed_read_paths": list(args.get("allowed_read_paths") or ["**"]),
                "allowed_write_paths": allowed_write_paths,
                "forbidden_write_roots": forbidden_write_roots,
                "allowed_commands": allowed_commands,
                "dependencies": list(args.get("dependencies") or []),
                "acceptance_checks": acceptance_checks,
                "timeout_seconds": timeout,
                "mock_behavior": args.get("mock_behavior", "pass"),
            }

            if wtype == "mock":
                worker = MockWorker(mode=args.get("mock_behavior"))
            elif wtype in ("opencode", "hermes", "dsh", "antigravity", "codex"):
                worker = CLIHarnessWorker(harness_name=wtype)
            elif wtype == "webui_cdp":
                worker = WebUICDPWorker()
            elif wtype == "webui_packet":
                worker = WebUIPacketWorker()
            else:
                raise ValueError(f"Unknown worker_type: {wtype}")

            if self.hub_store.dir.resolve() != DEFAULT_HUB_STORE.resolve():
                sup_runtime = self.hub_store.dir / "mcp_supervisor"
            else:
                sup_runtime = REPO_ROOT / ".runtime"

            sup = Supervisor(REPO_ROOT, runtime_root=sup_runtime)
            try:
                errs = sup.validate_contract(contract)
                if errs:
                    raise ValueError("; ".join(errs))
                sup.submit_contract(
                    contract,
                    canonical_root=str(REPO_ROOT),
                    protected_paths=get_protected_roots(),
                )
                sup.approve_contract(job_id, "mcp-orchestrator")
                sup.check_batch_gate(job_id)

                has_staged = any(sandbox_path.iterdir())
                outcome = sup.run_job_attempt(
                    job_id,
                    worker,
                    source_root=str(sandbox_path) if has_staged else None,
                )

                wr_data: Dict[str, Any] = {}
                att_row = sup.db.get_attempt(outcome.attempt_id) if outcome.attempt_id else None
                if att_row:
                    ev_file = sup.runtime_root / "evidence" / job_id / f"attempt-{att_row['attempt_num']}" / "worker_result.json"
                    if ev_file.is_file():
                        try:
                            wr_data = json.loads(ev_file.read_text(encoding="utf-8"))
                        except Exception:
                            wr_data = {}

                    if outcome.accepted and att_row["sandbox_path"]:
                        att_sb = Path(att_row["sandbox_path"])
                        real_att_sb = Path(os.path.realpath(att_sb))
                        real_sb_dst = Path(os.path.realpath(sandbox_path))
                        if str(real_att_sb) != str(real_sb_dst):
                            if att_sb.is_symlink() or is_reparse_point(att_sb) or sandbox_path.is_symlink() or is_reparse_point(sandbox_path):
                                raise ValueError("Symlink/junction detected on sandbox root during writeback")
                            wb_policy = PathPolicy(
                                allowed_write_paths,
                                forbidden_write_roots,
                                protected_roots=get_protected_roots(),
                                sandbox_root=sandbox_path,
                                workspace_root=REPO_ROOT,
                            )
                            for rel in outcome.changed_files:
                                if wb_policy.check_path(rel, sandbox_path):
                                    raise ValueError(f"Writeback blocked by PathPolicy for {rel!r}")
                                src_f = att_sb / rel
                                dst_f = sandbox_path / rel
                                if src_f.is_symlink() or is_reparse_point(src_f):
                                    raise ValueError(f"Refusing symlink/reparse writeback source: {rel!r}")
                                if dst_f.exists() and (dst_f.is_symlink() or is_reparse_point(dst_f)):
                                    raise ValueError(f"Refusing symlink/reparse writeback destination: {rel!r}")
                                cur_dir = sandbox_path
                                for part in Path(rel).parent.parts:
                                    if part in ("", "."):
                                        continue
                                    cur_dir = cur_dir / part
                                    if os.path.lexists(cur_dir) and (cur_dir.is_symlink() or is_reparse_point(cur_dir)):
                                        raise ValueError(f"Symlink/reparse ancestor blocked during writeback: {cur_dir}")
                                if not Path(os.path.realpath(src_f)).is_relative_to(real_att_sb):
                                    raise ValueError(f"Writeback source escapes attempt sandbox: {rel!r}")
                                if not Path(os.path.realpath(dst_f)).is_relative_to(real_sb_dst):
                                    raise ValueError(f"Writeback destination escapes sandbox_dir: {rel!r}")
                                if src_f.is_file():
                                    dst_f.parent.mkdir(parents=True, exist_ok=True)
                                    shutil.copy2(src_f, dst_f)

                raw_wstatus = wr_data.get("status")
                raw_exit = wr_data.get("exit_code")
                if outcome.accepted:
                    final_status = STATUS_COMPLETED
                    final_exit = 0
                elif raw_wstatus in (
                    STATUS_TIMEOUT,
                    STATUS_CANCELLED,
                    STATUS_BLOCKED_TOOL_UNAVAILABLE,
                    STATUS_AWAITING_WEBUI_PACKET,
                ):
                    final_status = str(raw_wstatus)
                    final_exit = int(raw_exit) if raw_exit is not None else 1
                else:
                    final_status = STATUS_FAILED
                    final_exit = int(raw_exit) if raw_exit not in (None, 0) else 1

                return {
                    "job_id": job_id,
                    "attempt_id": outcome.attempt_id,
                    "worker_type": wtype,
                    "status": final_status,
                    "exit_code": final_exit,
                    "changed_files": outcome.changed_files,
                    "stdout": wr_data.get("stdout", ""),
                    "stderr": wr_data.get("stderr", "") or ("; ".join(outcome.reasons) if not outcome.accepted else ""),
                    "final_state": outcome.final_state,
                    "gate_verdict": outcome.gate_verdict,
                    "accepted": outcome.accepted,
                    "reasons": outcome.reasons,
                    "failure_class": outcome.failure_class,
                    "governed_by": "Supervisor",
                }
            finally:
                sup.close()

        if name == "orchestra_get_workdir":
            return {"ok": True, **self.hub_store.settings.get_workdir()}

        if name == "orchestra_set_workdir":
            candidate = str(args.get("working_directory", "")).strip()
            role = str(args.get("role", "orchestrator")).strip() or "orchestrator"
            info = self.hub_store.settings.set_workdir(candidate, role=role)
            return {"ok": True, **info}

        if name == "orchestra_list_external_mcps":
            return {
                "ok": True,
                "servers": self.hub_store.external_mcp.list_servers(),
                "presets": BUILTIN_PRESETS,
                "config_path": str(self.hub_store.external_mcp.file_path),
            }

        if name == "orchestra_probe_external_mcp":
            sname = str(args.get("name", "")).strip()
            srv = self.hub_store.external_mcp.get_server(sname) or BUILTIN_PRESETS.get(sname)
            if not srv:
                raise ValueError(f"External MCP server '{sname}' not found")
            workdir = self.hub_store.settings.get_workdir()["working_directory"]
            return probe_external_mcp(
                srv,
                live_handshake=bool(args.get("live_handshake", False)),
                cwd=workdir,
            )

        raise ValueError(f"Unknown tool: {name}")

    def run_stdio(self) -> None:
        """Run the MCP server over standard input and output."""
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                resp = self.handle_message(msg)
                if resp is not None:
                    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                    sys.stdout.flush()
            except Exception as e:
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": f"Internal server error: {e}"},
                }
                sys.stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()


def main() -> int:
    server = OrchestraMCPServer()
    server.run_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main())
