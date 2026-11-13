"""Persistent SQLite settings store and PathGuard-enforced working directory manager."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.sandbox.path_guard import PathViolation
from src.sandbox.workdir_guard import (
    PROTECTED_ABS_ROOTS,
    PROTECTED_REL_SUBPATHS,
    validate_workdir,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ALLOWED_WORKER_MODELS = (
    "gemini-3.8-flash",
    "gemini-4-hydrogen",
    "claude-sonnet-5.5",
)
DEFAULT_POLICY_OVERRIDES: Dict[str, Any] = {
    "default_worker_model": "gemini-3.8-flash",
    "allow_argon_worker_override": False,
    "notes": "",
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class DashboardSettingsStore:
    """Thread-safe SQLite WAL store for Dashboard working directory, MCP mirror, and policy overrides."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        workspace_root: Path | str = REPO_ROOT,
    ):
        self.workspace_root = Path(os.path.realpath(os.path.abspath(str(workspace_root))))
        self.db_path = Path(db_path) if db_path else (REPO_ROOT / ".runtime" / "state" / "dashboard_settings.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise sqlite3.ProgrammingError("DashboardSettingsStore is closed")
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS dashboard_settings (
                        key TEXT PRIMARY KEY,
                        value_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS settings_audit (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        key TEXT NOT NULL,
                        role TEXT NOT NULL,
                        value_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """
                )
            finally:
                conn.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=5)
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.DatabaseError:
                    pass
                finally:
                    conn.close()
            except sqlite3.DatabaseError:
                pass
            finally:
                self._closed = True

    def _get_raw(self, key: str) -> Optional[tuple[Any, str]]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT value_json, updated_at FROM dashboard_settings WHERE key=?",
                    (key,),
                ).fetchone()
                if not row:
                    return None
                return json.loads(row["value_json"]), row["updated_at"]
            finally:
                conn.close()

    def _set_raw(self, key: str, value: Any, role: str = "operator") -> str:
        ts = _utc_iso()
        payload_str = json.dumps(value, ensure_ascii=False, sort_keys=True)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        """
                        INSERT INTO dashboard_settings(key, value_json, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            value_json=excluded.value_json,
                            updated_at=excluded.updated_at
                        """,
                        (key, payload_str, ts),
                    )
                    conn.execute(
                        "INSERT INTO settings_audit(key, role, value_json, updated_at) VALUES (?, ?, ?, ?)",
                        (key, role, payload_str, ts),
                    )
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
        return ts

    def get_workdir(self) -> Dict[str, str]:
        rec = self._get_raw("working_directory")
        if rec is not None:
            saved = rec[0]
            if isinstance(saved, str):
                try:
                    return validate_workdir(saved, self.workspace_root)
                except PathViolation:
                    pass
        return validate_workdir(".", self.workspace_root)

    def set_workdir(self, candidate: str, *, role: str = "operator") -> Dict[str, str]:
        info = validate_workdir(candidate, self.workspace_root)
        self._set_raw("working_directory", info["working_directory"], role=role)
        return info

    def get_model_policy_overrides(self) -> Dict[str, Any]:
        rec = self._get_raw("model_policy_overrides")
        base = dict(DEFAULT_POLICY_OVERRIDES)
        if rec is not None and isinstance(rec[0], dict):
            base.update(rec[0])
        return base

    def set_model_policy_overrides(self, overrides: Dict[str, Any], *, role: str) -> Dict[str, Any]:
        norm_role = (role or "operator").strip().lower()
        if norm_role != "reviewer":
            raise PermissionError("Forbidden: model_policy_overrides can only be modified with 'reviewer' role")
        if not isinstance(overrides, dict):
            raise ValueError("model_policy_overrides must be a JSON object")

        current = self.get_model_policy_overrides()
        default_model = str(overrides.get("default_worker_model", current["default_worker_model"])).strip()
        if default_model not in ALLOWED_WORKER_MODELS:
            raise ValueError(f"default_worker_model must be one of {ALLOWED_WORKER_MODELS}")

        allow_argon = bool(overrides.get("allow_argon_worker_override", current["allow_argon_worker_override"]))
        notes = str(overrides.get("notes", current.get("notes", "")))[:500]
        cleaned = {
            "default_worker_model": default_model,
            "allow_argon_worker_override": allow_argon,
            "notes": notes,
        }
        self._set_raw("model_policy_overrides", cleaned, role=norm_role)
        return cleaned

    def get_all_settings(self, external_registry: Any = None) -> Dict[str, Any]:
        workdir = self.get_workdir()
        policy = self.get_model_policy_overrides()
        ext_servers: Dict[str, Any] = {}
        if external_registry is not None:
            ext_servers = external_registry.read_map(redact_secrets=True)
        else:
            rec = self._get_raw("external_mcp_servers")
            if rec is not None and isinstance(rec[0], dict):
                from src.mcp.proxy_mcp import redact_env
                ext_servers = {}
                for sname, scfg in rec[0].items():
                    scopy = dict(scfg)
                    if "env" in scopy and isinstance(scopy["env"], dict):
                        scopy["env"] = redact_env(scopy["env"])
                    ext_servers[sname] = scopy

        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT MAX(updated_at) AS ts FROM dashboard_settings").fetchone()
                updated_at = row["ts"] if row and row["ts"] else _utc_iso()
            finally:
                conn.close()

        return {
            "ok": True,
            "working_directory": workdir["working_directory"],
            "workspace_root": workdir["workspace_root"],
            "relative_path": workdir["relative_path"],
            "external_mcp_servers": ext_servers,
            "model_policy_overrides": policy,
            "updated_at_utc": updated_at,
        }

    def update_settings(
        self,
        payload: Dict[str, Any],
        *,
        role: str = "operator",
        external_registry: Any = None,
    ) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Settings payload must be a JSON object")
        norm_role = (role or "operator").strip().lower()

        # Pre-validate all sections before mutating state
        validated_workdir: Optional[Dict[str, str]] = None
        if "working_directory" in payload and payload["working_directory"] is not None:
            validated_workdir = validate_workdir(str(payload["working_directory"]), self.workspace_root)

        if "model_policy_overrides" in payload and payload["model_policy_overrides"] is not None:
            if norm_role != "reviewer":
                raise PermissionError("Forbidden: model_policy_overrides requires 'reviewer' role")
            ov = payload["model_policy_overrides"]
            if not isinstance(ov, dict):
                raise ValueError("model_policy_overrides must be a JSON object")
            if "default_worker_model" in ov and str(ov["default_worker_model"]).strip() not in ALLOWED_WORKER_MODELS:
                raise ValueError(f"default_worker_model must be one of {ALLOWED_WORKER_MODELS}")

        ext_payload = payload.get("external_mcp_servers")
        if ext_payload is not None and external_registry is not None:
            external_registry.validate_map(ext_payload)

        # Apply validated updates
        if validated_workdir is not None:
            self._set_raw("working_directory", validated_workdir["working_directory"], role=norm_role)

        if ext_payload is not None:
            if external_registry is not None:
                external_registry.replace_map(ext_payload)
                saved_map = external_registry._read_raw().get("mcpServers", {})
            else:
                if not isinstance(ext_payload, dict):
                    raise ValueError("external_mcp_servers must be an object")
                from src.mcp.proxy_mcp import encrypt_env
                rec = self._get_raw("external_mcp_servers")
                existing_map = rec[0] if rec and isinstance(rec[0], dict) else {}
                saved_map = {}
                for sname, scfg in ext_payload.items():
                    if isinstance(scfg, dict):
                        scopy = dict(scfg)
                        existing_env = existing_map.get(sname, {}).get("env", {}) if isinstance(existing_map.get(sname), dict) else {}
                        scopy["env"] = encrypt_env(dict(scopy.get("env", {})), existing_env=existing_env)
                        saved_map[sname] = scopy
                    else:
                        saved_map[sname] = scfg
            self._set_raw("external_mcp_servers", saved_map, role=norm_role)

        if "model_policy_overrides" in payload and payload["model_policy_overrides"] is not None:
            self.set_model_policy_overrides(payload["model_policy_overrides"], role=norm_role)

        return self.get_all_settings(external_registry=external_registry)
