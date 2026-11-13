"""Local-only WebUI Relay and Orchestra Dashboard Server.

Binds strictly to 127.0.0.1. Zero external pip dependencies (Pure Python 3.14 stdlib).
Integrates all 5 WebUI providers (ChatGPT, Claude, Gemini, GLM, DeepSeek)
and 4 Local CLIs (Antigravity, OpenCode, Hermes, DeepSeek Harness).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.mcp.proxy_mcp import (
    BUILTIN_PRESETS,
    ExternalMCPRegistry,
    probe_external_mcp,
)
from src.sandbox.path_guard import PathViolation
from src.webui.settings_store import DashboardSettingsStore
from src.workers.webui_packet_worker import (
    DEFAULT_ROLES,
    DEFAULT_TASKS,
    PROTOCOL,
    PROVIDERS,
    compute_digest,
    iso_utc,
    make_bundle,
    parse_iso_utc,
    prompt_for,
    utc_now,
    verify_and_import_response,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
JOB_ID_RE = re.compile(r"^[0-9a-zA-Z_-]{8,64}$")
MAX_REQUEST_BYTES = 256 * 1024

CLIS = ("antigravity", "opencode", "hermes", "dsh")
ASSET_KINDS = ("skill", "tool", "hook", "knowledge")
HEALTH_MODELS = [
    "gemini-3.8-flash",
    "gemini-4-hydrogen",
    "claude-sonnet-5.5",
    "gemini-4-argon (review-only)",
]


class RegistryStore:
    """Atomic local asset registry for skills, tools, hooks, and knowledge references."""

    def __init__(self, root: Path):
        self.file_path = root / "registry.json"
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _read_all(self) -> List[Dict[str, Any]]:
        if not self.file_path.is_file():
            return []
        try:
            items = json.loads(self.file_path.read_text(encoding="utf-8"))
            return items if isinstance(items, list) else []
        except Exception:
            return []

    def _write_all(self, items: List[Dict[str, Any]]) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.file_path.parent,
            prefix=".reg_",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(items, tmp, ensure_ascii=False, indent=2)
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, self.file_path)

    def list_assets(
        self,
        kind: Optional[str] = None,
        project: Optional[str] = None,
        role: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            items = self._read_all()
            results = []
            for item in items:
                if kind and item.get("kind") != kind:
                    continue
                if project and item.get("project") not in (project, "shared"):
                    continue
                if role and role not in item.get("roles", []):
                    continue
                results.append(item)
            return results

    def add_asset(self, asset_data: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            items = self._read_all()
            ident = str(asset_data.get("id", "")).strip()
            kind = str(asset_data.get("kind", "knowledge")).strip()
            title = str(asset_data.get("title", "")).strip()
            content = str(asset_data.get("content", "")).strip()
            source = str(asset_data.get("source", "user-reviewed")).strip()
            project = str(asset_data.get("project", "shared")).strip()
            roles = list(asset_data.get("roles", ["B", "C", "D"]))
            version = str(asset_data.get("version", "1.0")).strip()

            if not ident or not title or not content:
                raise ValueError("Asset requires id, title, and content")
            if kind not in ASSET_KINDS:
                raise ValueError(f"Invalid asset kind '{kind}'. Must be one of {ASSET_KINDS}")

            entry = {
                "id": ident,
                "kind": kind,
                "title": title,
                "content": content,
                "source": source,
                "project": project,
                "roles": roles,
                "version": version,
                "updated_at_utc": iso_utc(utc_now()),
            }
            # Compute canonical SHA-256 digest
            canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            entry["sha256"] = hashlib.sha256(canonical).hexdigest()

            # Replace existing or append
            updated = False
            for idx, existing in enumerate(items):
                if existing.get("id") == ident and existing.get("project") == project and existing.get("kind") == kind:
                    items[idx] = entry
                    updated = True
                    break
            if not updated:
                items.append(entry)

            self._write_all(items)
            return entry


class HubStore:
    """Thread-safe persistent storage for WebUI relay jobs, settings, and external MCP configs."""

    def __init__(
        self,
        storage_dir: Path,
        *,
        workspace_root: Optional[Path] = None,
        external_mcp_path: Optional[Path] = None,
        settings_db_path: Optional[Path] = None,
    ):
        self.dir = Path(storage_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.registry = RegistryStore(self.dir)

        default_hub = (REPO_ROOT / ".runtime" / "webui_hub").resolve()
        is_default_hub = self.dir.resolve() == default_hub
        resolved_workspace = workspace_root or REPO_ROOT

        if settings_db_path is None:
            settings_db_path = (
                REPO_ROOT / ".runtime" / "state" / "dashboard_settings.db"
                if is_default_hub
                else self.dir / "dashboard_settings.db"
            )
        if external_mcp_path is None:
            external_mcp_path = (
                REPO_ROOT / ".runtime" / "mcp_configs" / "external.json"
                if is_default_hub
                else self.dir / "external_mcp.json"
            )

        self.settings = DashboardSettingsStore(db_path=settings_db_path, workspace_root=resolved_workspace)
        self.external_mcp = ExternalMCPRegistry(config_path=external_mcp_path, workspace_root=resolved_workspace)

    def close(self) -> None:
        self.settings.close()

    def _job_file(self, job_id: str) -> Path:
        if not JOB_ID_RE.fullmatch(job_id):
            raise ValueError(f"Invalid job identifier: {job_id}")
        return self.dir / f"job_{job_id}.json"

    def _write_state(self, job_id: str, state: Dict[str, Any]) -> None:
        target = self._job_file(job_id)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.dir,
            prefix=".job_",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(state, tmp, ensure_ascii=False, indent=2)
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, target)

    def _read_state(self, job_id: str) -> Dict[str, Any]:
        target = self._job_file(job_id)
        if not target.is_file():
            raise FileNotFoundError(f"Job {job_id} not found")
        with target.open("r", encoding="utf-8") as f:
            return json.load(f)

    def create_job(
        self,
        case_id: str,
        scenario: str,
        ttl_seconds: int = 300,
        job_id: Optional[str] = None,
        provider_map: Optional[Dict[str, str]] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            bundle = make_bundle(
                case_id=case_id,
                scenario=scenario,
                ttl_seconds=ttl_seconds,
                job_id=job_id,
                provider_map=provider_map,
            )
            state = {
                "bundle": bundle,
                "replies": {},
                "model": model or "gemini-3.8-flash",
                "created_utc": iso_utc(utc_now()),
            }
            self._write_state(bundle["job_id"], state)
            return self.view_job(bundle["job_id"])

    def view_job(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            state = self._read_state(job_id)
            bundle = state["bundle"]
            packets = bundle.get("packets", {})
            replies = state.get("replies", {})
            now = utc_now()

            slots = {}
            accepted: Dict[str, Any] = {}
            issues: List[str] = []

            for role, packet in packets.items():
                record = replies.get(role)
                if record:
                    rec_time = parse_iso_utc(record["received_utc"])
                    validation = verify_and_import_response(packet, record["response"], received_at=rec_time)
                    if validation.get("valid"):
                        accepted[role] = validation["data"]
                        slots[role] = {
                            "role": packet.get("role", role),
                            "provider": packet.get("provider", "chatgpt"),
                            "status": validation["data"].get("status", "completed"),
                            "result": validation["data"],
                            "received_utc": record["received_utc"],
                        }
                    else:
                        issues.extend(validation.get("issues", []))
                        slots[role] = {
                            "role": packet.get("role", role),
                            "provider": packet.get("provider", "chatgpt"),
                            "status": "invalid",
                            "error": validation.get("issues"),
                        }
                else:
                    deadline = parse_iso_utc(packet["deadline_at_utc"])
                    expired = now > deadline
                    slots[role] = {
                        "role": packet.get("role", role),
                        "provider": packet.get("provider", "chatgpt"),
                        "status": "expired" if expired else "pending",
                        "result": None,
                        "received_utc": None,
                    }

            # Overall status calculation
            any_expired = any(s["status"] == "expired" for s in slots.values())
            all_completed = len(accepted) == len(packets) and all(r.get("status") == "completed" for r in accepted.values())
            any_blocked = any(r.get("status") == "blocked" for r in accepted.values())

            if issues:
                overall_status = "INVALID_FAIL_CLOSED"
            elif all_completed:
                overall_status = "READY_FOR_A_REVIEW"
            elif any_blocked:
                overall_status = "BLOCKED_FAIL_CLOSED"
            elif any_expired:
                overall_status = "EXPIRED_FAIL_CLOSED"
            else:
                overall_status = "AWAITING_WEBUI_PACKET"

            missing = [r for r, s in slots.items() if s["status"] in ("pending", "expired")]

            prompts = {role: prompt_for(packet) for role, packet in packets.items()}

            return {
                "job_id": bundle["job_id"],
                "case_id": bundle["case_id"],
                "model": state.get("model", "gemini-3.8-flash"),
                "created_utc": state["created_utc"],
                "status": overall_status,
                "slots": slots,
                "prompts": prompts,
                "packets": packets,
                "issues": issues,
                "missing": missing,
                "decision_authority": "HUMAN_OPERATOR_REVIEW",
            }

    def list_jobs(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = []
            files = sorted(self.dir.glob("job_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for f in files[:limit]:
                try:
                    state = json.loads(f.read_text(encoding="utf-8"))
                    bundle = state.get("bundle", {})
                    jid = bundle.get("job_id")
                    if jid:
                        v = self.view_job(jid)
                        jobs.append({
                            "job_id": v["job_id"],
                            "case_id": v["case_id"],
                            "created_utc": v["created_utc"],
                            "status": v["status"],
                            "missing": v["missing"],
                        })
                except Exception:
                    continue
            return jobs

    def submit_reply(self, job_id: str, response: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            state = self._read_state(job_id)
            if not isinstance(response, dict):
                raise ValueError("Response must be a JSON object")

            role = response.get("context_id")
            bundle = state.get("bundle", {})
            packets = bundle.get("packets", {})

            if role not in packets:
                raise ValueError(f"Unknown context_id/role '{role}'. Job contains roles: {list(packets.keys())}")

            packet = packets[role]
            now = utc_now()
            validation = verify_and_import_response(packet, response, received_at=now)
            if not validation.get("valid"):
                raise ValueError(f"Response validation failed: {', '.join(validation.get('issues', []))}")

            state.setdefault("replies", {})[role] = {
                "response": response,
                "received_utc": iso_utc(now),
            }
            self._write_state(job_id, state)
            return self.view_job(job_id)


class DashboardHandler(BaseHTTPRequestHandler):
    """Strict HTTP request handler bound only to localhost."""

    store: HubStore

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Suppress default noisy stdio logging during test runs

    def _send_json(self, status: int, data: Any) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, file_path: Path, content_type: str) -> None:
        if not file_path.is_file():
            self.send_error(404, "File not found")
            return
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> Dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            return {}
        length = int(length_header)
        if length > MAX_REQUEST_BYTES:
            raise ValueError(f"Payload exceeds maximum allowed size ({MAX_REQUEST_BYTES} bytes)")
        body = self.rfile.read(length).decode("utf-8")
        return json.loads(body) if body.strip() else {}

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Health
        if path == "/api/health":
            workdir_info = self.store.settings.get_workdir()
            self._send_json(200, {
                "ok": True,
                "providers": list(PROVIDERS),
                "clis": list(CLIS),
                "models": list(HEALTH_MODELS),
                "working_directory": workdir_info["working_directory"],
                "timestamp_utc": iso_utc(utc_now()),
            })
            return

        # Active Working Directory
        if path == "/api/workdir":
            self._send_json(200, {"ok": True, **self.store.settings.get_workdir()})
            return

        # Dashboard Settings
        if path == "/api/settings":
            self._send_json(200, self.store.settings.get_all_settings(external_registry=self.store.external_mcp))
            return

        # External MCP Registry
        if path == "/api/mcp/external":
            self._send_json(200, {
                "ok": True,
                "servers": self.store.external_mcp.list_servers(),
                "presets": BUILTIN_PRESETS,
                "config_path": str(self.store.external_mcp.file_path),
            })
            return

        # List jobs
        if path == "/api/jobs":
            limit = int(query.get("limit", [30])[0])
            jobs = self.store.list_jobs(limit=limit)
            self._send_json(200, {"jobs": jobs})
            return

        # Get job
        if path.startswith("/api/jobs/"):
            job_id = path.removeprefix("/api/jobs/").strip("/")
            try:
                view = self.store.view_job(job_id)
                self._send_json(200, view)
            except FileNotFoundError:
                self._send_json(404, {"error": f"Job '{job_id}' not found"})
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
            return

        # Registry
        if path == "/api/registry":
            kind = query.get("kind", [None])[0]
            project = query.get("project", [None])[0]
            role = query.get("role", [None])[0]
            items = self.store.registry.list_assets(kind=kind, project=project, role=role)
            self._send_json(200, {"items": items})
            return

        # Static file delivery
        if path in ("/", "/index.html", "/dashboard.html"):
            self._send_file(STATIC_DIR / "dashboard.html", "text/html; charset=utf-8")
            return
        if path == "/dashboard.css":
            self._send_file(STATIC_DIR / "dashboard.css", "text/css; charset=utf-8")
            return
        if path == "/dashboard.js":
            self._send_file(STATIC_DIR / "dashboard.js", "application/javascript; charset=utf-8")
            return

        self.send_error(404, "Endpoint not found")

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path

        # Create job
        if path == "/api/jobs":
            try:
                body = self._read_json_body()
                case_id = str(body.get("case_id", "DISPATCH-001"))
                scenario = str(body.get("scenario", "")).strip()
                ttl = int(body.get("ttl", 300))
                job_id = body.get("job_id")
                provider_map = body.get("provider_map")
                model = body.get("model")
                job_view = self.store.create_job(
                    case_id=case_id,
                    scenario=scenario,
                    ttl_seconds=ttl,
                    job_id=job_id,
                    provider_map=provider_map,
                    model=model,
                )
                self._send_json(201, job_view)
            except Exception as e:
                self._send_json(400, {"error": str(e)})
            return

        # Submit response to job
        match_submit = re.fullmatch(r"/api/jobs/([0-9a-zA-Z_-]+)/(submit|reply)", path)
        if match_submit:
            job_id = match_submit.group(1)
            try:
                body = self._read_json_body()
                resp_payload = body.get("response", body)
                view = self.store.submit_reply(job_id, resp_payload)
                self._send_json(200, {
                    "ok": True,
                    "job_id": job_id,
                    "status": view["status"],
                    "accepted_role": resp_payload.get("context_id"),
                })
            except FileNotFoundError:
                self._send_json(404, {"error": f"Job '{job_id}' not found"})
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
            return

        # Registry add
        if path == "/api/registry":
            try:
                body = self._read_json_body()
                entry = self.store.registry.add_asset(body)
                self._send_json(201, {"ok": True, "asset": entry})
            except Exception as e:
                self._send_json(400, {"error": str(e)})
            return

        # Update Working Directory (strict PathGuard validation)
        if path == "/api/workdir":
            try:
                body = self._read_json_body()
                candidate = body.get("working_directory", body.get("workdir", body.get("path", "")))
                role = str(self.headers.get("X-Orchestra-Role") or body.get("role") or "operator")
                info = self.store.settings.set_workdir(candidate, role=role)
                self._send_json(200, {"ok": True, **info})
            except PathViolation as e:
                self._send_json(403, {"error": str(e), "code": e.code})
            except Exception as e:
                self._send_json(400, {"error": str(e)})
            return

        # Update Dashboard Settings (DB-backed; model_policy_overrides requires 'reviewer' role)
        if path == "/api/settings":
            try:
                body = self._read_json_body()
                role = str(
                    self.headers.get("X-Orchestra-Role")
                    or body.get("user_role")
                    or body.get("role")
                    or "operator"
                )
                updated = self.store.settings.update_settings(
                    body,
                    role=role,
                    external_registry=self.store.external_mcp,
                )
                self._send_json(200, updated)
            except PermissionError as e:
                self._send_json(403, {"error": str(e)})
            except PathViolation as e:
                self._send_json(403, {"error": str(e), "code": e.code})
            except Exception as e:
                self._send_json(400, {"error": str(e)})
            return

        # Register External MCP Server (stores in .runtime/mcp_configs/external.json + DB mirror)
        if path == "/api/mcp/external":
            try:
                body = self._read_json_body()
                entry = self.store.external_mcp.register_server(body)
                self.store.settings._set_raw(
                    "external_mcp_servers",
                    self.store.external_mcp.read_map(),
                    role="operator",
                )
                self._send_json(201, {
                    "ok": True,
                    "server": entry,
                    "servers": self.store.external_mcp.list_servers(),
                })
            except PathViolation as e:
                self._send_json(403, {"error": str(e), "code": e.code})
            except Exception as e:
                self._send_json(400, {"error": str(e)})
            return

        # Probe External MCP Server (e.g. Lnwjud)
        if path == "/api/mcp/external/probe":
            try:
                body = self._read_json_body()
                name = str(body.get("name", "")).strip()
                srv = None
                if name:
                    srv = self.store.external_mcp.get_server(name) or BUILTIN_PRESETS.get(name)
                if srv is None and body.get("command"):
                    srv = {
                        "name": name or "custom",
                        "command": body.get("command"),
                        "args": body.get("args", []),
                        "env": body.get("env", {}),
                        "enabled": True,
                        "working_directory": body.get("working_directory", ""),
                    }
                if srv is None:
                    self._send_json(404, {"error": f"External MCP server '{name}' not found"})
                    return
                workdir = self.store.settings.get_workdir()["working_directory"]
                probe = probe_external_mcp(
                    srv,
                    live_handshake=bool(body.get("live_handshake", False)),
                    timeout=float(body.get("timeout", 5.0)),
                    cwd=workdir,
                )
                self._send_json(200, {"ok": True, "probe": probe})
            except Exception as e:
                self._send_json(400, {"error": str(e)})
            return

        # Delete External MCP Server via POST fallback
        if path == "/api/mcp/external/delete":
            try:
                body = self._read_json_body()
                name = str(body.get("name", "")).strip()
                removed = self.store.external_mcp.remove_server(name)
                if not removed:
                    self._send_json(404, {"error": f"External MCP server '{name}' not found"})
                    return
                self.store.settings._set_raw(
                    "external_mcp_servers",
                    self.store.external_mcp.read_map(),
                    role="operator",
                )
                self._send_json(200, {"ok": True, "removed": name, "servers": self.store.external_mcp.list_servers()})
            except Exception as e:
                self._send_json(400, {"error": str(e)})
            return

        self.send_error(404, "Endpoint not found")

    def do_DELETE(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path.startswith("/api/mcp/external/"):
            name = path.removeprefix("/api/mcp/external/").strip("/")
            removed = self.store.external_mcp.remove_server(name)
            if not removed:
                self._send_json(404, {"error": f"External MCP server '{name}' not found"})
                return
            self.store.settings._set_raw(
                "external_mcp_servers",
                self.store.external_mcp.read_map(),
                role="operator",
            )
            self._send_json(200, {"ok": True, "removed": name, "servers": self.store.external_mcp.list_servers()})
            return
        self.send_error(404, "Endpoint not found")


class DashboardServer:
    """Thread-safe encapsulation of the local dashboard HTTP server."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        storage_dir: Optional[Path] = None,
        *,
        workspace_root: Optional[Path] = None,
        external_mcp_path: Optional[Path] = None,
        settings_db_path: Optional[Path] = None,
    ):
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("Dashboard server must bind strictly to 127.0.0.1")
        self.host = "127.0.0.1"
        self.requested_port = port
        self.storage_dir = storage_dir or (REPO_ROOT / ".runtime" / "webui_hub")
        self.store = HubStore(
            self.storage_dir,
            workspace_root=workspace_root,
            external_mcp_path=external_mcp_path,
            settings_db_path=settings_db_path,
        )

        class CustomHandler(DashboardHandler):
            store = self.store

        self.httpd = ThreadingHTTPServer((self.host, self.requested_port), CustomHandler)
        self.port = self.httpd.server_port
        self._thread: Optional[threading.Thread] = None

    def start_background(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=2)
        self.store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="WebUI Control AI Local Dashboard")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on (default 8765)")
    parser.add_argument("--dir", type=Path, default=None, help="Directory for storing job states")
    args = parser.parse_args()

    server = DashboardServer(port=args.port, storage_dir=args.dir)
    print(f"WebUI Orchestra Dashboard running at http://127.0.0.1:{server.port}/")
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard...")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
