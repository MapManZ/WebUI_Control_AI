"""Unit and integration tests for Dashboard UX upgrades, Working Directory PathGuard, Settings DB, and External MCP Proxy (Lnwjud)."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from src.mcp.orchestra_mcp import OrchestraMCPServer
from src.mcp.proxy_mcp import ExternalMCPProxy, ExternalMCPRegistry
from src.webui.dashboard_server import DashboardServer

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestDashboardTooltipsAndLayout(unittest.TestCase):
    """Verify Thai semantic tooltips, Active Working Directory display, and new tabs in Dashboard HTML."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.server = DashboardServer(port=0, storage_dir=Path(self.temp_dir.name))
        self.server.start_background()
        self.base_url = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.temp_dir.cleanup()

    def test_thai_tooltips_and_sections_injected_in_html(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/dashboard.html", timeout=5) as resp:
            html = resp.read().decode("utf-8")

        # Active Working Directory in sidebar & title bar
        self.assertIn('id="sidebar-workdir-path"', html)
        self.assertIn('id="topbar-workdir-path"', html)
        self.assertIn("Active Working Directory", html)

        # Tabs for MCP Integrations and Settings
        self.assertIn('data-tab="mcp"', html)
        self.assertIn('data-tab="settings"', html)
        self.assertIn('id="tab-mcp"', html)
        self.assertIn('id="tab-settings"', html)

        # Thai semantic tooltips on all major sections
        expected_thai_snippets = [
            "แถบเมนูด้านข้าง (Sidebar)",
            "สร้างใบสั่งงานใหม่ (New Orchestra Assignment)",
            "รหัสอ้างอิงเคสงาน (Case Identifier)",
            "ระดับโมเดล (Model Tier)",
            "คลังข้อมูลอ้างอิงกลาง (Shared References)",
            "จัดการการเชื่อมต่อ MCP ภายนอก",
            "ตั้งค่าระบบ (Settings)",
            "โฟลเดอร์ทำงานปัจจุบัน (Active Working Directory)",
        ]
        for snippet in expected_thai_snippets:
            self.assertIn(snippet, html, f"Missing Thai tooltip snippet: {snippet}")
        self.assertIn("data-tooltip=", html)
        self.assertIn('class="tooltip-badge"', html)


class TestWorkdirAndSettingsDB(unittest.TestCase):
    """Verify /api/workdir PathGuard validation and /api/settings DB updates + reviewer role gate."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.db_path = self.tmp_path / "settings.db"
        self.ext_path = self.tmp_path / "external.json"
        self.server = DashboardServer(
            port=0,
            storage_dir=self.tmp_path,
            workspace_root=REPO_ROOT,
            settings_db_path=self.db_path,
            external_mcp_path=self.ext_path,
        )
        self.server.start_background()
        self.base_url = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.temp_dir.cleanup()

    def _request(
        self,
        path: str,
        method: str = "GET",
        data: dict | None = None,
        headers: dict | None = None,
    ) -> tuple[int, dict]:
        url = self.base_url + path
        req_headers = dict(headers or {})
        if data is not None:
            req_headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, headers=req_headers, data=body, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def test_workdir_get_and_set_within_workspace_and_pathguard_blocks(self) -> None:
        status, info = self._request("/api/workdir")
        self.assertEqual(status, 200)
        self.assertTrue(info["ok"])
        self.assertEqual(Path(info["workspace_root"]).resolve(), REPO_ROOT.resolve())

        # Valid subdir inside workspace
        valid_sub = str(REPO_ROOT / "src" / "webui")
        status, updated = self._request("/api/workdir", method="POST", data={"working_directory": valid_sub})
        self.assertEqual(status, 200)
        self.assertEqual(Path(updated["working_directory"]).resolve(), (REPO_ROOT / "src" / "webui").resolve())
        self.assertEqual(updated["relative_path"], "src/webui")

        # Forbidden paths outside workspace or protected internal paths
        forbidden_candidates = [
            r"O:\AI_Workspace\REA Project",
            r"O:\AI_Workspace\REA Project\OpenCodeREA\SummoningRemake",
            r"C:\Windows",
            "../../outside",
            str(REPO_ROOT / ".git"),
            str(REPO_ROOT / ".runtime" / "keys"),
            str(REPO_ROOT / ".runtime" / "state"),
            str(REPO_ROOT / ".runtime" / "evidence"),
        ]
        for bad_path in forbidden_candidates:
            st, err = self._request("/api/workdir", method="POST", data={"working_directory": bad_path})
            self.assertEqual(st, 403, f"Expected 403 PathGuard block for {bad_path}, got {st}: {err}")
            self.assertIn("code", err)

    def test_settings_api_updates_sqlite_db_and_enforces_reviewer_role(self) -> None:
        # Non-reviewer cannot update model_policy_overrides
        st, err = self._request(
            "/api/settings",
            method="POST",
            headers={"X-Orchestra-Role": "operator"},
            data={
                "working_directory": str(REPO_ROOT / "src"),
                "model_policy_overrides": {
                    "default_worker_model": "gemini-4-hydrogen",
                    "allow_argon_worker_override": False,
                    "notes": "unauthorized attempt",
                },
            },
        )
        self.assertEqual(st, 403)
        self.assertIn("reviewer", err["error"].lower())

        # Reviewer CAN update working_directory, external_mcp_servers, and model_policy_overrides
        payload = {
            "working_directory": str(REPO_ROOT / "src"),
            "external_mcp_servers": {
                "lnwjud": {
                    "command": "npx",
                    "args": ["-y", "@engasnm111/lnwjud"],
                    "enabled": True,
                    "description": "Lnwjud MCP",
                }
            },
            "model_policy_overrides": {
                "default_worker_model": "gemini-4-hydrogen",
                "allow_argon_worker_override": False,
                "notes": "Approved by reviewer",
            },
        }
        st, saved = self._request(
            "/api/settings",
            method="POST",
            headers={"X-Orchestra-Role": "reviewer"},
            data=payload,
        )
        self.assertEqual(st, 200, f"Settings save failed: {saved}")
        self.assertEqual(Path(saved["working_directory"]).resolve(), (REPO_ROOT / "src").resolve())
        self.assertEqual(saved["model_policy_overrides"]["default_worker_model"], "gemini-4-hydrogen")
        self.assertIn("lnwjud", saved["external_mcp_servers"])

        # Verify SQLite DB rows were actually persisted
        conn = sqlite3.connect(str(self.db_path))
        try:
            rows = dict(conn.execute("SELECT key, value_json FROM dashboard_settings").fetchall())
            self.assertIn("working_directory", rows)
            self.assertIn("model_policy_overrides", rows)
            self.assertIn("external_mcp_servers", rows)
            self.assertEqual(
                Path(json.loads(rows["working_directory"])).resolve(),
                (REPO_ROOT / "src").resolve(),
            )
            self.assertEqual(
                json.loads(rows["model_policy_overrides"])["default_worker_model"],
                "gemini-4-hydrogen",
            )
        finally:
            conn.close()

        # Verify external.json was also written
        ext_doc = json.loads(self.ext_path.read_text(encoding="utf-8"))
        self.assertIn("lnwjud", ext_doc["mcpServers"])


class TestLnwjudAndExternalMCPIntegration(unittest.TestCase):
    """Verify External MCP registration, probing (Lnwjud), live stdio JSON-RPC proxy, and Orchestra MCP workdir tools."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.db_path = self.tmp_path / "settings.db"
        self.ext_path = self.tmp_path / "external.json"
        self.server = DashboardServer(
            port=0,
            storage_dir=self.tmp_path,
            workspace_root=REPO_ROOT,
            settings_db_path=self.db_path,
            external_mcp_path=self.ext_path,
        )
        self.server.start_background()
        self.base_url = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.temp_dir.cleanup()

    def _request(self, path: str, method: str = "GET", data: dict | None = None) -> tuple[int, dict]:
        url = self.base_url + path
        headers = {"Content-Type": "application/json"} if data is not None else {}
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, headers=headers, data=body, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def test_register_and_probe_lnwjud_and_block_port_9121(self) -> None:
        # Register Lnwjud via API
        st, reg = self._request(
            "/api/mcp/external",
            method="POST",
            data={
                "name": "lnwjud",
                "command": "npx",
                "args": ["-y", "@engasnm111/lnwjud"],
                "enabled": True,
                "description": "Lnwjud MCP server",
            },
        )
        self.assertEqual(st, 201)
        self.assertTrue(reg["ok"])
        self.assertEqual(reg["server"]["command"], "npx")
        self.assertEqual(reg["server"]["args"], ["-y", "@engasnm111/lnwjud"])

        # Probe Lnwjud (executable presence check, offline-safe)
        st, probe_res = self._request(
            "/api/mcp/external/probe",
            method="POST",
            data={"name": "lnwjud", "live_handshake": False},
        )
        self.assertEqual(st, 200)
        self.assertTrue(probe_res["ok"])
        self.assertEqual(probe_res["probe"]["name"], "lnwjud")
        self.assertTrue(probe_res["probe"]["available"], f"Expected npx available on machine: {probe_res}")

        # Safety block on port 9121 or REA Project
        st_bad, err_bad = self._request(
            "/api/mcp/external",
            method="POST",
            data={
                "name": "bad-serena",
                "command": "python",
                "args": ["--port", "9121"],
            },
        )
        self.assertEqual(st_bad, 400)
        self.assertIn("9121", err_bad["error"])

    def test_proxy_mcp_live_stdio_bridge(self) -> None:
        # Create a small local stdio MCP mock server script inside temp_dir
        mock_srv = self.tmp_path / "mock_ext_mcp.py"
        mock_srv.write_text(
            """import json, os, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid = msg.get("id")
    m = msg.get("method")
    if mid is None:
        continue
    if m == "initialize":
        resp = {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "mock-lnwjud", "version": "0.1.0"}}}
    elif m == "tools/list":
        resp = {"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": "echo_cwd", "description": "Echo cwd and input", "inputSchema": {"type": "object"}}]}}
    elif m == "tools/call":
        args = msg.get("params", {}).get("arguments", {})
        resp = {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps({"cwd": os.getcwd(), "echo": args})}], "isError": False}}
    else:
        resp = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "not found"}}
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
""",
            encoding="utf-8",
        )

        reg = ExternalMCPRegistry(config_path=self.ext_path, workspace_root=REPO_ROOT)
        reg.register_server({
            "name": "mock_ext",
            "command": sys.executable,
            "args": [str(mock_srv)],
            "enabled": True,
            "working_directory": str(REPO_ROOT / "src"),
        })

        proxy = ExternalMCPProxy(config_path=self.ext_path, workspace_root=REPO_ROOT)
        tools_resp = proxy.handle_message({
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/list",
            "params": {"discover_external": True},
        })
        tool_names = [t["name"] for t in tools_resp["result"]["tools"]]
        self.assertIn("mock_ext__echo_cwd", tool_names)

        call_resp = proxy.handle_message({
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "mock_ext__echo_cwd", "arguments": {"msg": "hello-lnwjud"}},
        })
        self.assertFalse(call_resp["result"]["isError"])
        payload = json.loads(call_resp["result"]["content"][0]["text"])
        self.assertEqual(payload["echo"], {"msg": "hello-lnwjud"})
        self.assertEqual(Path(payload["cwd"]).resolve(), (REPO_ROOT / "src").resolve())

    def test_orchestra_mcp_get_and_set_workdir(self) -> None:
        mcp = OrchestraMCPServer(
            hub_store_dir=self.tmp_path,
            workspace_root=REPO_ROOT,
            settings_db_path=self.db_path,
            external_mcp_path=self.ext_path,
        )
        list_resp = mcp.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        names = [t["name"] for t in list_resp["result"]["tools"]]
        self.assertIn("orchestra_get_workdir", names)
        self.assertIn("orchestra_set_workdir", names)

        set_resp = mcp.handle_message({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "orchestra_set_workdir",
                "arguments": {"working_directory": "src/mcp", "role": "lnwjud"},
            },
        })
        self.assertFalse(set_resp["result"]["isError"])
        set_data = json.loads(set_resp["result"]["content"][0]["text"])
        self.assertEqual(set_data["relative_path"], "src/mcp")

        get_resp = mcp.handle_message({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "orchestra_get_workdir", "arguments": {}},
        })
        self.assertFalse(get_resp["result"]["isError"])
        get_data = json.loads(get_resp["result"]["content"][0]["text"])
        self.assertEqual(Path(get_data["working_directory"]).resolve(), (REPO_ROOT / "src" / "mcp").resolve())

        # Blocked path outside workspace via MCP
        bad_resp = mcp.handle_message({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "orchestra_set_workdir",
                "arguments": {"working_directory": r"O:\AI_Workspace\REA Project"},
            },
        })
        self.assertTrue(bad_resp["result"]["isError"])

    def test_default_external_mcp_path_is_runtime_external_json(self) -> None:
        reg = ExternalMCPRegistry()
        self.assertEqual(
            reg.file_path.resolve(),
            (REPO_ROOT / ".runtime" / "mcp_configs" / "external.json").resolve(),
        )


if __name__ == "__main__":
    unittest.main()
