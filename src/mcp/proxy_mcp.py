"""Dynamic External MCP Proxy & Registry (supports Lnwjud and custom stdio MCP servers).

Stores server configurations in `.runtime/mcp_configs/external.json` and exposes
both programmatic probing/bridging and a JSON-RPC 2.0 stdio MCP proxy server.
Zero external pip dependencies (Python 3.14 standard library only).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.sandbox.workdir_guard import validate_workdir
from src.workers.cli_harness_worker import kill_process_tree

DEFAULT_EXTERNAL_MCP_PATH = REPO_ROOT / ".runtime" / "mcp_configs" / "external.json"
SERVER_NAME_RE = re.compile(r"^[0-9a-zA-Z_-]{2,64}$")
FORBIDDEN_MARKERS = ("9121", "rea project", "summoningremake")

BUILTIN_PRESETS: Dict[str, Dict[str, Any]] = {
    "lnwjud": {
        "name": "lnwjud",
        "command": "npx",
        "args": ["-y", "@engasnm111/lnwjud"],
        "env": {},
        "enabled": True,
        "description": "Lnwjud External MCP Server (@engasnm111/lnwjud)",
        "working_directory": "",
    },
}


def _check_safety(text: str, field_name: str) -> None:
    lowered = text.lower()
    for marker in FORBIDDEN_MARKERS:
        if marker in lowered:
            raise ValueError(f"Safety block: '{field_name}' cannot reference protected resource ({marker})")


def validate_server_entry(
    name: str,
    raw: Dict[str, Any],
    *,
    workspace_root: Path = REPO_ROOT,
) -> Dict[str, Any]:
    """Normalize and strictly validate a single external MCP server configuration."""
    clean_name = str(name or "").strip()
    if not SERVER_NAME_RE.fullmatch(clean_name):
        raise ValueError(f"Invalid external MCP server name '{clean_name}' (use 2-64 chars: a-z, A-Z, 0-9, _, -)")
    if not isinstance(raw, dict):
        raise ValueError(f"Server '{clean_name}' configuration must be a JSON object")

    command = str(raw.get("command", "")).strip()
    if not command or "\x00" in command:
        raise ValueError(f"Server '{clean_name}' requires a valid non-empty 'command'")
    _check_safety(command, f"{clean_name}.command")

    raw_args = raw.get("args", [])
    if not isinstance(raw_args, list):
        raise ValueError(f"Server '{clean_name}' 'args' must be a list of strings")
    args: List[str] = []
    for item in raw_args:
        s = str(item)
        if "\x00" in s:
            raise ValueError(f"Server '{clean_name}' args contain NUL byte")
        _check_safety(s, f"{clean_name}.args")
        args.append(s)

    raw_env = raw.get("env", {})
    if raw_env is None:
        raw_env = {}
    if not isinstance(raw_env, dict):
        raise ValueError(f"Server '{clean_name}' 'env' must be an object")
    env: Dict[str, str] = {}
    for k, v in raw_env.items():
        ks = str(k).strip()
        vs = str(v)
        if not ks or "\x00" in ks or "\x00" in vs:
            raise ValueError(f"Server '{clean_name}' env contains invalid key/value")
        _check_safety(vs, f"{clean_name}.env.{ks}")
        env[ks] = vs

    workdir = str(raw.get("working_directory", "") or "").strip()
    if workdir:
        _check_safety(workdir, f"{clean_name}.working_directory")
        workdir = validate_workdir(workdir, workspace_root)["working_directory"]

    return {
        "name": clean_name,
        "command": command,
        "args": args,
        "env": env,
        "enabled": bool(raw.get("enabled", True)),
        "description": str(raw.get("description", "") or "").strip()[:300],
        "working_directory": workdir,
    }


class ExternalMCPRegistry:
    """Thread-safe atomic registry storing external MCP servers in `.runtime/mcp_configs/external.json`."""

    def __init__(self, config_path: Optional[Path] = None, workspace_root: Path = REPO_ROOT):
        self.file_path = Path(config_path) if config_path else DEFAULT_EXTERNAL_MCP_PATH
        self.workspace_root = Path(os.path.realpath(os.path.abspath(str(workspace_root))))
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _read_raw(self) -> Dict[str, Any]:
        if not self.file_path.is_file():
            return {"mcpServers": {}}
        try:
            data = json.loads(self.file_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
                return data
            if isinstance(data, dict):
                return {"mcpServers": data}
        except Exception:
            pass
        return {"mcpServers": {}}

    def _write_raw(self, data: Dict[str, Any]) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.file_path.parent,
            prefix=".ext_mcp_",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(data, tmp, ensure_ascii=False, indent=2)
            tmp.write("\n")
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, self.file_path)

    def read_map(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            raw = self._read_raw().get("mcpServers", {})
            out: Dict[str, Dict[str, Any]] = {}
            for name, cfg in raw.items():
                if isinstance(cfg, dict):
                    out[name] = {
                        "name": name,
                        "command": cfg.get("command", ""),
                        "args": list(cfg.get("args", [])),
                        "env": dict(cfg.get("env", {})),
                        "enabled": bool(cfg.get("enabled", True)),
                        "description": cfg.get("description", ""),
                        "working_directory": cfg.get("working_directory", ""),
                    }
            return out

    def list_servers(self) -> List[Dict[str, Any]]:
        return list(self.read_map().values())

    def get_server(self, name: str) -> Optional[Dict[str, Any]]:
        return self.read_map().get(name)

    def register_server(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            name = str(data.get("name", "")).strip()
            entry = validate_server_entry(name, data, workspace_root=self.workspace_root)
            doc = self._read_raw()
            servers = doc.setdefault("mcpServers", {})
            stored = {k: v for k, v in entry.items() if k != "name"}
            servers[name] = stored
            self._write_raw(doc)
            return entry

    def remove_server(self, name: str) -> bool:
        clean_name = str(name or "").strip()
        with self._lock:
            doc = self._read_raw()
            servers = doc.get("mcpServers", {})
            if clean_name not in servers:
                return False
            del servers[clean_name]
            self._write_raw(doc)
            return True

    def validate_map(self, servers_map: Any) -> Dict[str, Dict[str, Any]]:
        if not isinstance(servers_map, dict):
            raise ValueError("external_mcp_servers must be an object mapping server names to configs")
        validated: Dict[str, Dict[str, Any]] = {}
        for name, cfg in servers_map.items():
            entry = validate_server_entry(name, cfg, workspace_root=self.workspace_root)
            validated[entry["name"]] = {k: v for k, v in entry.items() if k != "name"}
        return validated

    def replace_map(self, servers_map: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            validated = self.validate_map(servers_map)
            self._write_raw({"mcpServers": validated})
            return self.read_map()


def _resolve_command(command: str) -> Optional[str]:
    if os.path.isabs(command) and Path(command).is_file():
        return command
    resolved = shutil.which(command)
    if resolved:
        return resolved
    if os.name == "nt" and "." not in Path(command).name:
        for ext in (".cmd", ".exe", ".bat"):
            candidate = shutil.which(command + ext)
            if candidate:
                return candidate
    return None


def _stdio_rpc_exchange(
    command: str,
    args: List[str],
    requests: List[Dict[str, Any]],
    *,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
    timeout: float = 5.0,
) -> List[Dict[str, Any]]:
    """Send JSON-RPC 2.0 requests line-by-line to a stdio MCP server and collect responses."""
    resolved_cmd = _resolve_command(command) or command
    proc_env = os.environ.copy()
    if env:
        proc_env.update({str(k): str(v) for k, v in env.items()})
    proc_env.setdefault("PYTHONUTF8", "1")

    stdin_payload = "".join(json.dumps(req, ensure_ascii=False) + "\n" for req in requests)
    proc = subprocess.Popen(
        [resolved_cmd, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd or str(REPO_ROOT),
        env=proc_env,
    )
    try:
        stdout, stderr = proc.communicate(input=stdin_payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        try:
            proc.communicate(timeout=2)
        except Exception:
            pass
        raise TimeoutError(f"External MCP server timed out after {timeout}s")
    finally:
        if proc.poll() is None:
            kill_process_tree(proc.pid)

    responses: List[Dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and "jsonrpc" in parsed:
                responses.append(parsed)
        except ValueError:
            continue

    if not responses and proc.returncode not in (0, None):
        err_detail = (stderr or "").strip() or f"Process exited with code {proc.returncode}"
        raise RuntimeError(err_detail)
    return responses


def probe_external_mcp(
    server_config: Dict[str, Any],
    *,
    live_handshake: bool = False,
    timeout: float = 5.0,
    cwd: Optional[str] = None,
) -> Dict[str, Any]:
    """Probe an external MCP server for executable readiness and optional JSON-RPC tools/list handshake."""
    name = str(server_config.get("name", "external")).strip() or "external"
    command = str(server_config.get("command", "")).strip()
    args = [str(a) for a in server_config.get("args", [])]
    env = dict(server_config.get("env", {}))
    enabled = bool(server_config.get("enabled", True))
    effective_cwd = str(server_config.get("working_directory") or cwd or REPO_ROOT)

    try:
        _check_safety(command, "command")
        for a in args:
            _check_safety(a, "args")
    except ValueError as exc:
        return {
            "name": name,
            "command": command,
            "args": args,
            "enabled": enabled,
            "available": False,
            "executable_path": "",
            "handshake_attempted": False,
            "handshake_ok": None,
            "tools": [],
            "server_info": None,
            "error": str(exc),
        }

    resolved_exe = _resolve_command(command) if command else None
    result: Dict[str, Any] = {
        "name": name,
        "command": command,
        "args": args,
        "enabled": enabled,
        "available": bool(resolved_exe),
        "executable_path": resolved_exe or "",
        "handshake_attempted": bool(live_handshake and resolved_exe),
        "handshake_ok": None,
        "tools": [],
        "server_info": None,
        "error": None if resolved_exe else f"Executable not found on PATH: {command}",
    }

    if not resolved_exe or not live_handshake:
        return result

    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "webui-control-ai-proxy", "version": "1.0.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    try:
        responses = _stdio_rpc_exchange(
            resolved_exe,
            args,
            requests,
            env=env,
            cwd=effective_cwd,
            timeout=timeout,
        )
        init_resp = next((r for r in responses if r.get("id") == 1), None)
        tools_resp = next((r for r in responses if r.get("id") == 2), None)
        if init_resp and "result" in init_resp:
            result["server_info"] = init_resp["result"].get("serverInfo")
        if tools_resp and "result" in tools_resp:
            result["tools"] = tools_resp["result"].get("tools", [])
        result["handshake_ok"] = bool(init_resp and "result" in init_resp)
        if not result["handshake_ok"]:
            result["error"] = "No valid JSON-RPC initialize response received"
    except Exception as exc:
        result["handshake_ok"] = False
        result["error"] = str(exc)

    return result


def call_external_mcp_tool(
    server_config: Dict[str, Any],
    tool_name: str,
    arguments: Optional[Dict[str, Any]] = None,
    *,
    timeout: float = 15.0,
    cwd: Optional[str] = None,
) -> Dict[str, Any]:
    """Forward a single `tools/call` invocation to an external stdio MCP server."""
    command = str(server_config.get("command", "")).strip()
    args = [str(a) for a in server_config.get("args", [])]
    env = dict(server_config.get("env", {}))
    effective_cwd = str(server_config.get("working_directory") or cwd or REPO_ROOT)

    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "webui-control-ai-proxy", "version": "1.0.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        },
    ]
    responses = _stdio_rpc_exchange(
        command,
        args,
        requests,
        env=env,
        cwd=effective_cwd,
        timeout=timeout,
    )
    call_resp = next((r for r in responses if r.get("id") == 2), None)
    if call_resp is None:
        raise RuntimeError(f"External MCP server did not return a tools/call response for '{tool_name}'")
    if "error" in call_resp:
        raise RuntimeError(f"External MCP error: {call_resp['error']}")
    return call_resp.get("result", {})


BUILTIN_PROXY_TOOLS = [
    {
        "name": "proxy_list_servers",
        "description": "List all registered external MCP servers (including Lnwjud) from external.json.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "proxy_probe_server",
        "description": "Probe an external MCP server by name (or preset like 'lnwjud').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Registered server name or preset"},
                "live_handshake": {"type": "boolean", "description": "Perform stdio JSON-RPC initialize + tools/list"},
            },
            "required": ["name"],
        },
    },
]


class ExternalMCPProxy:
    """JSON-RPC 2.0 stdio MCP proxy dynamically bridging registered external MCP servers."""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        workspace_root: Path = REPO_ROOT,
        default_cwd: Optional[str] = None,
    ):
        self.registry = ExternalMCPRegistry(config_path=config_path, workspace_root=workspace_root)
        self.workspace_root = Path(os.path.realpath(os.path.abspath(str(workspace_root))))
        self.default_cwd = default_cwd or str(self.workspace_root)

    def list_all_tools(self, *, discover_external: bool = False, timeout: float = 3.0) -> List[Dict[str, Any]]:
        tools = list(BUILTIN_PROXY_TOOLS)
        if not discover_external:
            return tools
        for srv in self.registry.list_servers():
            if not srv.get("enabled", True):
                continue
            probe = probe_external_mcp(srv, live_handshake=True, timeout=timeout, cwd=self.default_cwd)
            for ext_tool in probe.get("tools", []):
                tname = ext_tool.get("name")
                if not tname:
                    continue
                tools.append({
                    "name": f"{srv['name']}__{tname}",
                    "description": f"[{srv['name']}] {ext_tool.get('description', '')}".strip(),
                    "inputSchema": ext_tool.get("inputSchema", {"type": "object", "properties": {}}),
                })
        return tools

    def handle_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(message, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}

        msg_id = message.get("id")
        method = message.get("method")
        params = message.get("params", {}) or {}

        if msg_id is None:
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "orchestra-proxy-mcp", "version": "1.0.0"},
                },
            }

        if method == "tools/list":
            discover = bool(params.get("discover_external", False))
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": self.list_all_tools(discover_external=discover)},
            }

        if method == "tools/call":
            tool_name = str(params.get("name", ""))
            args = params.get("arguments", {}) or {}
            try:
                if tool_name == "proxy_list_servers":
                    payload = {"servers": self.registry.list_servers(), "presets": BUILTIN_PRESETS}
                    return {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                            "isError": False,
                        },
                    }
                if tool_name == "proxy_probe_server":
                    sname = str(args.get("name", "")).strip()
                    srv = self.registry.get_server(sname) or BUILTIN_PRESETS.get(sname)
                    if not srv:
                        raise ValueError(f"External MCP server '{sname}' not found")
                    probe = probe_external_mcp(
                        srv,
                        live_handshake=bool(args.get("live_handshake", False)),
                        cwd=self.default_cwd,
                    )
                    return {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": json.dumps(probe, ensure_ascii=False, indent=2)}],
                            "isError": False,
                        },
                    }
                if "__" in tool_name:
                    server_name, remote_tool = tool_name.split("__", 1)
                    srv = self.registry.get_server(server_name)
                    if not srv:
                        raise ValueError(f"External MCP server '{server_name}' is not registered")
                    if not srv.get("enabled", True):
                        raise ValueError(f"External MCP server '{server_name}' is disabled")
                    forwarded = call_external_mcp_tool(srv, remote_tool, args, cwd=self.default_cwd)
                    return {"jsonrpc": "2.0", "id": msg_id, "result": forwarded}
                raise ValueError(f"Unknown proxy tool: {tool_name}")
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Proxy tool failed: {exc}"}],
                        "isError": True,
                    },
                }

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method '{method}' not found"},
        }

    def run_stdio(self) -> None:
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
            except Exception as exc:
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": f"Internal proxy error: {exc}"},
                }
                sys.stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()


def main() -> int:
    proxy = ExternalMCPProxy()
    proxy.run_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main())
