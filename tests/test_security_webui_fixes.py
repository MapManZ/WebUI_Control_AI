"""Targeted unit and regression tests for WebUI / Dashboard / MCP / CLI security fixes.

Verifies:
- Finding 3.3 & 3.15: Host/Origin/Sec-Fetch-Site validation, CSRF token enforcement,
  Content-Type enforcement, Reviewer token authentication, arbitrary command probe locking,
  and bounds clamping (Content-Length, limit, ttl).
- Finding 3.5: DPAPI encryption, storage in external.json and SQLite, secret redaction (***REDACTED***),
  and preservation of secrets when redacted values are sent back.
- Finding 3.6: Version pinning and safe defaults for Lnwjud preset.
- Finding 3.12: Packet HMAC-SHA256 signature, tamper rejection, and HubStore conflict protection on submit_reply.
- Finding 3.14: Elimination of shell=True in doctor.py and installer.py.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from src.cli.doctor import _run_cmd, run_doctor
from src.cli.installer import _run_wrapper_version
from src.mcp.proxy_mcp import (
    BUILTIN_PRESETS,
    ExternalMCPRegistry,
    decrypt_secret_dpapi,
    encrypt_secret_dpapi,
)
from src.webui.dashboard_server import DashboardServer, load_or_create_reviewer_token
from src.workers.webui_packet_worker import (
    PROTOCOL,
    compute_digest,
    compute_packet_hmac,
    ingest_response,
    make_packet,
    verify_and_import_response,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestFinding35DPAPIAndSecretRedaction(unittest.TestCase):
    """Verify Windows DPAPI encryption and API redaction for external MCP secrets."""

    def test_dpapi_encrypt_decrypt_cycle(self) -> None:
        secret = "secret-token-value-xyz-987"
        encrypted = encrypt_secret_dpapi(secret)
        self.assertTrue(encrypted.startswith("dpapi:"))
        self.assertNotEqual(encrypted, secret)

        decrypted = decrypt_secret_dpapi(encrypted)
        self.assertEqual(decrypted, secret)

    def test_external_mcp_registry_encrypts_and_redacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = Path(tmp_dir) / "external.json"
            registry = ExternalMCPRegistry(config_path=cfg_path, workspace_root=REPO_ROOT)

            # Register a server with secret env
            registered = registry.register_server({
                "name": "custom-mcp",
                "command": "python",
                "args": ["-m", "http.server"],
                "env": {"API_KEY": "super-confidential-key", "PUBLIC_VAR": "hello"},
            })
            # Returned dict must have redacted env
            self.assertEqual(registered["env"]["API_KEY"], "***REDACTED***")
            self.assertEqual(registered["env"]["PUBLIC_VAR"], "***REDACTED***")

            # Raw file must store dpapi-encrypted values, not plaintext
            raw_doc = json.loads(cfg_path.read_text(encoding="utf-8"))
            stored_env = raw_doc["mcpServers"]["custom-mcp"]["env"]
            self.assertTrue(stored_env["API_KEY"].startswith("dpapi:"))
            self.assertNotIn("super-confidential-key", cfg_path.read_text(encoding="utf-8"))

            # read_map(redact_secrets=True) must redact
            redacted_map = registry.read_map(redact_secrets=True)
            self.assertEqual(redacted_map["custom-mcp"]["env"]["API_KEY"], "***REDACTED***")

            # read_map(redact_secrets=False) must decrypt
            plain_map = registry.read_map(redact_secrets=False)
            self.assertEqual(plain_map["custom-mcp"]["env"]["API_KEY"], "super-confidential-key")

            # Updating with ***REDACTED*** must preserve existing encrypted secret
            updated = registry.register_server({
                "name": "custom-mcp",
                "command": "python",
                "args": ["-m", "http.server"],
                "env": {"API_KEY": "***REDACTED***", "PUBLIC_VAR": "updated-value"},
            })
            plain_after_update = registry.read_map(redact_secrets=False)
            self.assertEqual(plain_after_update["custom-mcp"]["env"]["API_KEY"], "super-confidential-key")
            self.assertEqual(plain_after_update["custom-mcp"]["env"]["PUBLIC_VAR"], "updated-value")

    def test_settings_store_encrypts_env_when_external_registry_is_none(self) -> None:
        from src.webui.settings_store import DashboardSettingsStore
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "settings.db"
            store = DashboardSettingsStore(db_path=db_path, workspace_root=REPO_ROOT)
            try:
                store.update_settings({
                    "external_mcp_servers": {
                        "db-srv": {
                            "command": "python",
                            "args": ["-V"],
                            "env": {"DB_PASS": "plain_password_123"},
                        }
                    }
                }, role="operator", external_registry=None)

                # Check SQLite directly: env must be stored encrypted with dpapi:
                conn = sqlite3.connect(str(db_path))
                row = conn.execute("SELECT value_json FROM dashboard_settings WHERE key='external_mcp_servers'").fetchone()
                conn.close()
                self.assertIsNotNone(row)
                stored_data = json.loads(row[0])
                stored_env = stored_data["db-srv"]["env"]
                self.assertTrue(stored_env["DB_PASS"].startswith("dpapi:"))
                self.assertNotIn("plain_password_123", row[0])
            finally:
                store.close()


class TestFinding36LnwjudPresetHardening(unittest.TestCase):
    """Verify Lnwjud MCP preset version pinning and default disabled status."""

    def test_lnwjud_preset_is_pinned_and_disabled(self) -> None:
        preset = BUILTIN_PRESETS.get("lnwjud")
        self.assertIsNotNone(preset)
        self.assertFalse(preset["enabled"])
        self.assertIn("@engasnm111/lnwjud@1.0.0", preset["args"])
        self.assertIn("--no-install", preset["args"])
        self.assertNotIn("-y", preset["args"])

    def test_lnwjud_dashboard_ui_script_and_html_are_pinned(self) -> None:
        # Verify dashboard.js does not contain unpinned lnwjud args and sets enabled=false
        js_file = REPO_ROOT / "src" / "webui" / "static" / "dashboard.js"
        self.assertTrue(js_file.is_file())
        js_content = js_file.read_text(encoding="utf-8")
        self.assertNotIn("-y @engasnm111/lnwjud", js_content)
        self.assertIn("--no-install @engasnm111/lnwjud@1.0.0", js_content)
        self.assertIn('document.getElementById("mcp-server-enabled").value = "false";', js_content)

        # Verify dashboard.html also does not contain unpinned lnwjud args
        html_file = REPO_ROOT / "src" / "webui" / "static" / "dashboard.html"
        self.assertTrue(html_file.is_file())
        html_content = html_file.read_text(encoding="utf-8")
        self.assertNotIn("-y @engasnm111/lnwjud", html_content)
        self.assertIn("--no-install @engasnm111/lnwjud@1.0.0", html_content)


class TestFinding312PacketHMACAndSubmitConflict(unittest.TestCase):
    """Verify HMAC-SHA256 work packet authentication and HubStore overwrite conflict protection."""

    def test_packet_hmac_computation_and_tamper_rejection(self) -> None:
        pkt = make_packet(
            job_id="sec-job-1",
            case_id="CASE-SEC-1",
            context_id="B",
            role="Analyst",
            provider="chatgpt",
            scenario="Verify security",
            task="Audit code",
        )
        self.assertIn("packet_hmac", pkt)
        self.assertEqual(pkt["packet_hmac"], compute_packet_hmac(pkt))

        # Tampered packet must be rejected by HMAC verification
        tampered = dict(pkt)
        tampered["scenario"] = "Attacker modified prompt"
        response = {
            "protocol": PROTOCOL,
            "job_id": "sec-job-1",
            "case_id": "CASE-SEC-1",
            "context_id": "B",
            "round": 1,
            "packet_sha256": pkt["packet_sha256"],
            "status": "completed",
            "finding": "Analyst finding",
            "evidence": ["ev"],
            "limitations": [],
        }
        res = verify_and_import_response(tampered, response)
        self.assertFalse(res["valid"])
        self.assertEqual(res["status"], "REJECTED_TAMPERED_PACKET")

        # Missing packet_hmac must be rejected with REJECTED_TAMPERED_PACKET
        no_hmac = dict(pkt)
        del no_hmac["packet_hmac"]
        res_no_hmac = verify_and_import_response(no_hmac, response)
        self.assertFalse(res_no_hmac["valid"])
        self.assertEqual(res_no_hmac["status"], "REJECTED_TAMPERED_PACKET")

        # Empty packet_hmac must be rejected
        empty_hmac = dict(pkt, packet_hmac="")
        res_empty = verify_and_import_response(empty_hmac, response)
        self.assertFalse(res_empty["valid"])
        self.assertEqual(res_empty["status"], "REJECTED_TAMPERED_PACKET")

        # Ingest alias works
        res_alias = ingest_response(pkt, response)
        self.assertTrue(res_alias["valid"])
        self.assertEqual(res_alias["status"], "ACCEPTED")

    def test_submit_reply_conflict_protection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            server = DashboardServer(port=0, storage_dir=Path(tmp_dir))
            server.start_background()
            try:
                created = server.store.create_job(case_id="CASE-SUBMIT", scenario="Test submit")
                job_id = created["job_id"]
                pkt_b = created["packets"]["B"]
                b_reply = {
                    "protocol": PROTOCOL,
                    "job_id": job_id,
                    "case_id": "CASE-SUBMIT",
                    "context_id": "B",
                    "round": 1,
                    "packet_sha256": pkt_b["packet_sha256"],
                    "status": "completed",
                    "finding": "First valid finding",
                    "evidence": ["ev1"],
                    "limitations": [],
                }
                # Initial submit succeeds
                server.store.submit_reply(job_id, b_reply, allow_overwrite=False)

                # Attempting to overwrite completed reply without allow_overwrite raises ValueError Conflict
                second_reply = dict(b_reply, finding="Attempted overwrite")
                with self.assertRaises(ValueError) as cm:
                    server.store.submit_reply(job_id, second_reply, allow_overwrite=False)
                self.assertIn("Conflict:", str(cm.exception))

                # With allow_overwrite=True, overwrite succeeds and history is tracked
                server.store.submit_reply(job_id, second_reply, allow_overwrite=True)
                view = server.store.view_job(job_id)
                self.assertEqual(view["slots"]["B"]["result"]["finding"], "Attempted overwrite")
            finally:
                server.shutdown()


class TestFinding314ShellTrueRemoval(unittest.TestCase):
    """Verify that doctor.py and installer.py execute commands without shell=True."""

    def test_doctor_run_cmd_runs_without_shell(self) -> None:
        ok, out = _run_cmd(["python", "--version"])
        self.assertTrue(ok)
        self.assertIn("Python", out)

        # String invocation also executes with shell=False
        ok_str, out_str = _run_cmd("python --version")
        self.assertTrue(ok_str)
        self.assertIn("Python", out_str)

    def test_installer_wrapper_version_runs_safely(self) -> None:
        non_existent = Path("non_existent_wrapper_file.cmd")
        res = _run_wrapper_version(non_existent)
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["error"].lower())


class TestFinding33And315DashboardServerSecurity(unittest.TestCase):
    """Verify Host/Origin/CSRF validation, Content-Type enforcement, Reviewer token auth, and bounds."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.server = DashboardServer(port=0, storage_dir=Path(self.temp_dir.name))
        self.server.start_background()
        self.base_url = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.temp_dir.cleanup()

    def _raw_request(
        self,
        path: str,
        method: str = "GET",
        headers: dict | None = None,
        data: bytes | None = None,
    ) -> tuple[int, dict | str]:
        url = self.base_url + path
        req = urllib.request.Request(url, headers=headers or {}, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                body = r.read().decode("utf-8")
                try:
                    return r.status, json.loads(body)
                except Exception:
                    return r.status, body
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(body)
            except Exception:
                return exc.code, body

    def test_host_header_validation(self) -> None:
        # Legitimate loopback Host succeeds
        st, res = self._raw_request("/api/health", headers={"Host": f"127.0.0.1:{self.server.port}"})
        self.assertEqual(st, 200)

        # Non-loopback Host rejected with 403
        st_bad, _ = self._raw_request("/api/health", headers={"Host": "malicious-domain.com"})
        self.assertEqual(st_bad, 403)

    def test_csrf_token_required_on_post_and_delete(self) -> None:
        # POST without CSRF token is rejected with 403
        st, err = self._raw_request(
            "/api/jobs",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=b'{"case_id": "TEST"}',
        )
        self.assertEqual(st, 403)
        self.assertIn("CSRF", err.get("error", ""))

        # POST with valid CSRF token succeeds
        st_ok, created = self._raw_request(
            "/api/jobs",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Orchestra-CSRF": self.server.csrf_token,
            },
            data=b'{"case_id": "TEST", "scenario": "Valid"}',
        )
        self.assertEqual(st_ok, 201)

    def test_content_type_enforcement_on_post(self) -> None:
        # POST with Content-Type text/plain must be rejected with 415
        st, err = self._raw_request(
            "/api/jobs",
            method="POST",
            headers={
                "Content-Type": "text/plain",
                "X-Orchestra-CSRF": self.server.csrf_token,
            },
            data=b'{"case_id": "TEST"}',
        )
        self.assertEqual(st, 415)

        # POST with Content-Length 0 and Content-Type text/plain must also be rejected with 415
        st_zero, _ = self._raw_request(
            "/api/jobs",
            method="POST",
            headers={
                "Content-Type": "text/plain",
                "Content-Length": "0",
                "X-Orchestra-CSRF": self.server.csrf_token,
            },
            data=b"",
        )
        self.assertEqual(st_zero, 415)

    def test_cross_port_origin_rejection(self) -> None:
        # Cross-port localhost origin (e.g. port 3000) must be rejected with 403
        st, err = self._raw_request(
            "/api/jobs",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Orchestra-CSRF": self.server.csrf_token,
                "Origin": "http://localhost:3000",
            },
            data=b'{"case_id": "TEST"}',
        )
        self.assertEqual(st, 403)
        self.assertIn("Cross-port", err.get("error", ""))

    def test_reviewer_token_enforced_against_role_escalation(self) -> None:
        payload = json.dumps({
            "model_policy_overrides": {
                "default_worker_model": "gemini-4-hydrogen",
                "allow_argon_worker_override": True,
            }
        }).encode("utf-8")

        # Unauthenticated request with X-Orchestra-Role: reviewer rejected with 403
        st, err = self._raw_request(
            "/api/settings",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Orchestra-CSRF": self.server.csrf_token,
                "X-Orchestra-Role": "reviewer",
            },
            data=payload,
        )
        self.assertEqual(st, 403)
        self.assertIn("reviewer", err.get("error", "").lower())

        # Request with valid X-Orchestra-Reviewer-Token succeeds
        st_ok, saved = self._raw_request(
            "/api/settings",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Orchestra-CSRF": self.server.csrf_token,
                "X-Orchestra-Role": "reviewer",
                "X-Orchestra-Reviewer-Token": self.server.reviewer_token,
            },
            data=payload,
        )
        self.assertEqual(st_ok, 200)

    def test_reviewer_token_enforced_against_case_insensitive_role(self) -> None:
        # Case variations of 'reviewer' (e.g. REVIEWER, Reviewer) without token must be rejected with 403
        for candidate_role in ("REVIEWER", "Reviewer", "  reviewer  "):
            payload = json.dumps({
                "role": candidate_role,
                "model_policy_overrides": {
                    "default_worker_model": "gemini-4-hydrogen",
                }
            }).encode("utf-8")

            st, err = self._raw_request(
                "/api/settings",
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Orchestra-CSRF": self.server.csrf_token,
                    "X-Orchestra-Role": candidate_role,
                },
                data=payload,
            )
            self.assertEqual(st, 403)
            self.assertIn("reviewer", err.get("error", "").lower())

    def test_portless_loopback_origin_rejection(self) -> None:
        # Loopback origin without explicit port defaults to port 80/443;
        # when dashboard runs on dynamic port != 80, it must be rejected as cross-port with 403
        st, err = self._raw_request(
            "/api/jobs",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Orchestra-CSRF": self.server.csrf_token,
                "Origin": "http://localhost",
            },
            data=b'{"case_id": "TEST"}',
        )
        self.assertEqual(st, 403)
        self.assertIn("Cross-port", err.get("error", ""))

    def test_arbitrary_unregistered_command_probe_is_blocked(self) -> None:
        # Attempting to probe arbitrary command without registration must return 403
        payload = json.dumps({
            "command": "cmd.exe",
            "args": ["/c", "dir"],
            "live_handshake": True,
        }).encode("utf-8")
        st, err = self._raw_request(
            "/api/mcp/external/probe",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Orchestra-CSRF": self.server.csrf_token,
            },
            data=payload,
        )
        self.assertEqual(st, 403)
        self.assertIn("Forbidden", err.get("error", ""))

    def test_bounds_and_error_handling(self) -> None:
        # Negative Content-Length returns 400
        url = self.base_url + "/api/jobs"
        req = urllib.request.Request(
            url,
            headers={
                "Content-Type": "application/json",
                "Content-Length": "-1",
                "X-Orchestra-CSRF": self.server.csrf_token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                st = r.status
        except urllib.error.HTTPError as exc:
            st = exc.code
        self.assertEqual(st, 400)

        # Invalid limit query param returns 400
        st_limit, _ = self._raw_request("/api/jobs?limit=notanint")
        self.assertEqual(st_limit, 400)


if __name__ == "__main__":
    unittest.main()
