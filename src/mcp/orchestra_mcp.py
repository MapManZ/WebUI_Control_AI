"""Universal MCP Server for WebUI Control AI.

Enables ANY agent (OpenCode, Antigravity, Hermes, DSH, Claude, ChatGPT)
to act as Orchestrator or Worker over standard JSON-RPC 2.0 stdio MCP.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import uuid

from src.workers.base import (
    STATUS_COMPLETED,
    STATUS_FAILED,
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
            limit = int(args.get("limit", 20))
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
            job_id = args.get("job_id") or uuid.uuid4().hex
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
            job_id = args["job_id"]
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
            wtype = args["worker_type"].lower()
            goal = args["goal"]
            job_id = args.get("job_id", f"job-{uuid.uuid4().hex[:8]}")
            sandbox_dir = args.get("sandbox_dir") or str(REPO_ROOT / ".runtime" / "sandboxes" / job_id)
            timeout = int(args.get("timeout_seconds", 120))

            contract = {
                "job_id": job_id,
                "goal": goal,
                "timeout_seconds": timeout,
                "mock_behavior": args.get("mock_behavior", "pass"),
                "allowed_write_paths": ["solution.txt", "result.txt", "output.txt"],
            }

            attempt_id = f"{job_id}-att-1"

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

            res = worker.start(contract, sandbox_dir, attempt_id)
            return res.to_dict()

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
