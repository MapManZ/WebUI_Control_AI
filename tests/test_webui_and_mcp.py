"""Comprehensive unit and integration tests for WebUI Relay, Dashboard, and MCP server."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.mcp.orchestra_mcp import OrchestraMCPServer
from src.webui.dashboard_server import DashboardServer
from src.workers.base import (
    STATUS_AWAITING_WEBUI_PACKET,
    STATUS_COMPLETED,
    WorkerResult,
)
from src.workers.webui_cdp_worker import (
    WebUICDPWorker,
    probe_webui_targets,
)
from src.workers.webui_packet_worker import (
    PROTOCOL,
    PROVIDERS,
    compute_digest,
    iso_utc,
    make_bundle,
    make_packet,
    prompt_for,
    utc_now,
    verify_and_import_response,
)


class TestWebUIPacketWorker(unittest.TestCase):
    """Test packet generation, verification, and tamper rejection across all 5 WebUI providers."""

    def test_all_5_providers_packet_generation(self) -> None:
        for prov in PROVIDERS:
            packet = make_packet(
                job_id="test-job-001",
                case_id="CASE-001",
                context_id="B",
                role="Analyst",
                provider=prov,
                scenario="Audit system security",
                task="Review architecture",
                ttl_seconds=300,
            )
            self.assertEqual(packet["protocol"], PROTOCOL)
            self.assertEqual(packet["provider"], prov)
            self.assertIn("packet_sha256", packet)
            # Verify digest
            self.assertEqual(packet["packet_sha256"], compute_digest(packet))

            # Verify prompt contains packet and role
            prompt = prompt_for(packet)
            self.assertIn(prov.upper(), prompt)
            self.assertIn(packet["packet_sha256"], prompt)

    def test_verify_and_import_valid_response(self) -> None:
        packet = make_packet(
            job_id="job-val-1",
            case_id="case-1",
            context_id="C",
            role="Verifier",
            provider="claude",
            scenario="Verify numbers",
            task="Check calculation",
        )
        response = {
            "protocol": PROTOCOL,
            "job_id": "job-val-1",
            "case_id": "case-1",
            "context_id": "C",
            "round": 1,
            "packet_sha256": packet["packet_sha256"],
            "status": "completed",
            "finding": "Calculations are completely sound and verified.",
            "evidence": ["Evidence point 1", "Evidence point 2"],
            "limitations": ["Limited to provided sample dataset"],
        }
        res = verify_and_import_response(packet, response)
        self.assertTrue(res["valid"])
        self.assertEqual(res["status"], "ACCEPTED")

    def test_tamper_rejection_sha256(self) -> None:
        packet = make_packet(
            job_id="job-tamper-1",
            case_id="case-1",
            context_id="B",
            role="Analyst",
            provider="chatgpt",
            scenario="Original text",
            task="Check",
        )
        # Tamper with packet
        tampered_packet = dict(packet)
        tampered_packet["scenario"] = "Attacker injected prompt modification"

        response = {
            "protocol": PROTOCOL,
            "job_id": "job-tamper-1",
            "case_id": "case-1",
            "context_id": "B",
            "round": 1,
            "packet_sha256": packet["packet_sha256"],
            "status": "completed",
            "finding": "Analysis",
            "evidence": ["ev"],
            "limitations": [],
        }
        res = verify_and_import_response(tampered_packet, response)
        self.assertFalse(res["valid"])
        self.assertEqual(res["status"], "REJECTED_TAMPERED_PACKET")

    def test_deadline_expired_rejection(self) -> None:
        now = utc_now()
        # Create packet issued in the past with short TTL
        past = now - timedelta(seconds=600)
        packet = make_packet(
            job_id="job-exp-1",
            case_id="case-1",
            context_id="D",
            role="Reviewer",
            provider="gemini",
            scenario="Scenario",
            task="Review",
            ttl_seconds=60,
            now=past,
        )
        response = {
            "protocol": PROTOCOL,
            "job_id": "job-exp-1",
            "case_id": "case-1",
            "context_id": "D",
            "round": 1,
            "packet_sha256": packet["packet_sha256"],
            "status": "completed",
            "finding": "Late reply",
            "evidence": ["ev"],
            "limitations": [],
        }
        res = verify_and_import_response(packet, response, received_at=now)
        self.assertFalse(res["valid"])
        self.assertIn("after deadline", str(res["issues"]))


class TestWebUICDPWorker(unittest.TestCase):
    """Test CDP worker and automatic fallback to packet copy-paste mode."""

    def test_cdp_fallback_when_port_closed(self) -> None:
        # Port 59999 is closed
        worker = WebUICDPWorker(cdp_port=59999, approve_send=True)
        with tempfile.TemporaryDirectory() as tmp:
            contract = {
                "job_id": "job-cdp-fallback",
                "goal": "Test CDP Fallback",
                "timeout_seconds": 60,
            }
            res = worker.start(contract, tmp, "att-cdp-1")
            self.assertEqual(res.status, STATUS_AWAITING_WEBUI_PACKET)
            self.assertEqual(res.metadata.get("fallback"), "dashboard_copy_paste")
            self.assertIn("prompt_job-cdp-fallback_B.txt", res.changed_files)


class TestDashboardServer(unittest.TestCase):
    """Test HTTP dashboard server on an ephemeral port (127.0.0.1:0)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.server = DashboardServer(port=0, storage_dir=Path(self.temp_dir.name))
        self.server.start_background()
        self.base_url = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.temp_dir.cleanup()

    def _request(self, path: str, method: str = "GET", data: dict | None = None) -> dict:
        url = self.base_url + path
        headers = {"Content-Type": "application/json"} if data is not None else {}
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, headers=headers, data=body, method=method)
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_get_health(self) -> None:
        health = self._request("/api/health")
        self.assertTrue(health.get("ok"))
        self.assertEqual(len(health["providers"]), 5)
        self.assertIn("glm", health["providers"])
        self.assertIn("deepseek", health["providers"])
        self.assertEqual(len(health["clis"]), 4)

    def test_job_lifecycle_and_submission(self) -> None:
        # Create job
        payload = {
            "case_id": "CASE-TEST-001",
            "scenario": "Verify mathematical proof",
            "ttl": 300,
            "provider_map": {"B": "claude", "C": "chatgpt", "D": "gemini"},
        }
        created = self._request("/api/jobs", method="POST", data=payload)
        job_id = created["job_id"]
        self.assertEqual(created["status"], "AWAITING_WEBUI_PACKET")

        # View job
        viewed = self._request(f"/api/jobs/{job_id}")
        self.assertEqual(viewed["job_id"], job_id)
        self.assertIn("B", viewed["prompts"])

        # Submit valid reply for B
        pkt_b = viewed["packets"]["B"]
        b_reply = {
            "protocol": PROTOCOL,
            "job_id": job_id,
            "case_id": "CASE-TEST-001",
            "context_id": "B",
            "round": 1,
            "packet_sha256": pkt_b["packet_sha256"],
            "status": "completed",
            "finding": "Analyst findings for proof",
            "evidence": ["E1"],
            "limitations": [],
        }
        sub_res = self._request(f"/api/jobs/{job_id}/submit", method="POST", data={"response": b_reply})
        self.assertTrue(sub_res.get("ok"))
        self.assertEqual(sub_res.get("accepted_role"), "B")

    def test_registry_endpoints(self) -> None:
        # Add asset
        asset_payload = {
            "kind": "knowledge",
            "id": "safety-rules",
            "title": "Safety Boundary Rules",
            "project": "shared",
            "content": "Rule 1: Never touch foreign workspace outside sandbox.",
            "roles": ["B", "C", "D"],
            "version": "1.0",
        }
        add_res = self._request("/api/registry", method="POST", data=asset_payload)
        self.assertTrue(add_res.get("ok"))
        self.assertIn("sha256", add_res["asset"])

        # List assets
        list_res = self._request("/api/registry?kind=knowledge")
        items = list_res.get("items", [])
        self.assertTrue(any(i["id"] == "safety-rules" for i in items))


class TestOrchestraMCPServer(unittest.TestCase):
    """Test stdio JSON-RPC 2.0 MCP server tools and dispatch."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.server = OrchestraMCPServer(hub_store_dir=Path(self.temp_dir.name))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_mcp_initialize(self) -> None:
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {},
        }
        resp = self.server.handle_message(req)
        self.assertEqual(resp["id"], 1)
        self.assertEqual(resp["result"]["serverInfo"]["name"], "orchestra-mcp")

    def test_mcp_tools_list(self) -> None:
        req = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
        resp = self.server.handle_message(req)
        tools = resp["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        self.assertIn("orchestra_doctor", tool_names)
        self.assertIn("orchestra_list_jobs", tool_names)
        self.assertIn("orchestra_create_webui_packet", tool_names)
        self.assertIn("orchestra_submit_webui_response", tool_names)
        self.assertIn("orchestra_probe_cli", tool_names)
        self.assertIn("orchestra_run_job", tool_names)

    def test_mcp_tool_call_doctor(self) -> None:
        req = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "orchestra_doctor",
                "arguments": {"detailed": True},
            },
        }
        resp = self.server.handle_message(req)
        self.assertFalse(resp["result"]["isError"])
        content_text = resp["result"]["content"][0]["text"]
        data = json.loads(content_text)
        self.assertEqual(data["status"], "ok")
        self.assertIn("clis", data)
        self.assertIn("webuis", data)

    def test_mcp_tool_call_create_webui_packet(self) -> None:
        req = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "orchestra_create_webui_packet",
                "arguments": {
                    "case_id": "MCP-CASE-001",
                    "provider": "deepseek",
                    "context_id": "B",
                    "role": "Analyst",
                    "scenario": "Evaluate algorithm complexity",
                },
            },
        }
        resp = self.server.handle_message(req)
        self.assertFalse(resp["result"]["isError"])
        data = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(data["provider"], "deepseek")
        self.assertIn("packet_sha256", data)

    def test_mcp_tool_call_run_mock_job(self) -> None:
        with tempfile.TemporaryDirectory() as sandbox:
            req = {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "orchestra_run_job",
                    "arguments": {
                        "job_id": "mcp-mock-job",
                        "worker_type": "mock",
                        "mock_behavior": "pass",
                        "goal": "Generate sample solution",
                        "sandbox_dir": sandbox,
                    },
                },
            }
            resp = self.server.handle_message(req)
            self.assertFalse(resp["result"]["isError"])
            res_dict = json.loads(resp["result"]["content"][0]["text"])
            self.assertEqual(res_dict["status"], STATUS_COMPLETED)
            self.assertEqual(res_dict["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
