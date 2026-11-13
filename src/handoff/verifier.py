"""Machine verification of handoff files (on import / reconstruction)."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from src.state import fsm
from src.state.db import StateDB, contract_digest

from .generator import (
    CONTEXT_DENSITIES, SCHEMA, SECTION_TITLES, changed_files_digest, receipt_digest, sign,
)

_RECEIPT_RE = re.compile(r"```json receipt\n(.*?)\n```", re.S)


@dataclass
class VerificationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    receipt: dict | None = None
    receipt_sha256: str | None = None
    handoff_file_sha256: str | None = None
    signed: bool = False


def parse_handoff(text: str) -> tuple[dict | None, str | None]:
    m = _RECEIPT_RE.search(text)
    if not m:
        return None, "receipt JSON block missing"
    try:
        return json.loads(m.group(1)), None
    except json.JSONDecodeError as exc:
        return None, f"receipt JSON unparsable: {exc}"


def verify_handoff(path: str | Path, *, db: StateDB | None = None, hmac_key: bytes | None = None,
                   require_signature: bool = False) -> VerificationResult:
    p = Path(path)
    errors: list[str] = []
    if not p.is_file():
        return VerificationResult(False, [f"handoff file not found: {p}"])
    raw = p.read_bytes()
    text = raw.decode("utf-8", "replace")
    fsha = hashlib.sha256(raw).hexdigest()
    for i, title in enumerate(SECTION_TITLES, 1):
        if f"## {i}. {title}" not in text:
            errors.append(f"mandatory section missing: {i}. {title}")
    receipt, err = parse_handoff(text)
    if err:
        return VerificationResult(False, errors + [err], None, None, fsha)
    if receipt.get("schema") != SCHEMA:
        errors.append(f"unknown schema {receipt.get('schema')!r}")
    for key in ("job_id", "next_job_id", "project", "contract_sha256", "contract", "changed_files",
                "changed_files_digest", "qa_commands", "rejected_attempts", "protected_boundary",
                "context_density", "evidence_manifest_sha256", "attempt_id"):
        if key not in receipt:
            errors.append(f"receipt field missing: {key}")
    if errors:
        return VerificationResult(False, errors, receipt, None, fsha)
    rsha = receipt_digest(receipt)
    m = re.search(r"Receipt SHA-256: `([0-9a-f]{64})`", text)
    if not m or m.group(1) != rsha:
        errors.append("Receipt SHA-256 line does not match receipt contents (tampered)")
    # human-readable section must agree with the machine receipt
    m = re.search(r"Contract SHA-256: `([0-9a-f]{64})`", text)
    if not m or m.group(1) != receipt["contract_sha256"]:
        errors.append("rendered contract SHA-256 differs from receipt")
    if contract_digest(receipt["contract"]) != receipt["contract_sha256"]:
        errors.append("embedded contract does not hash to contract_sha256")
    if changed_files_digest(receipt["changed_files"]) != receipt["changed_files_digest"]:
        errors.append("changed_files_digest does not match changed_files")
    m = re.search(r"Changed Files Digest: `([0-9a-f]{64})`", text)
    if not m or m.group(1) != receipt["changed_files_digest"]:
        errors.append("rendered changed-files digest differs from receipt")
    m = re.search(r"Context Density: (\w+)", text)
    if not m or m.group(1) not in CONTEXT_DENSITIES or m.group(1) != receipt["context_density"]:
        errors.append("context density missing/invalid (must be Low|Moderate|High|Unknown)")
    for c in receipt["qa_commands"]:
        if f"`{c['command']}` -> exit_code={c['exit_code']}" not in text:
            errors.append(f"rendered QA command differs from receipt: {c['command']}")
        if c.get("exit_code") != 0:
            errors.append(f"handoff records non-zero QA exit code for {c['command']}")
    if not receipt["qa_commands"]:
        errors.append("handoff has no QA commands")
    if not receipt["protected_boundary"].get("untouched"):
        errors.append("protected boundary was not untouched")
    if not receipt["protected_boundary"].get("allowlist_diff_ok"):
        errors.append("allowlist diff was not ok")
    sm = re.search(r"Signature \(HMAC-SHA256\): `([^`]+)`", text)
    signed = bool(sm and sm.group(1) != "UNSIGNED")
    if hmac_key is not None:
        if not signed:
            errors.append("handoff is unsigned but a verification key was supplied")
        elif not hmac.compare_digest(sm.group(1), sign(rsha, hmac_key)):
            errors.append("HMAC signature invalid (forged or tampered handoff)")
    elif require_signature:
        errors.append("signature required but no key supplied")
    if db is not None:
        errors.extend(_verify_against_db(db, receipt))
    return VerificationResult(not errors, errors, receipt, rsha, fsha, signed)


def _verify_against_db(db: StateDB, r: dict) -> list[str]:
    errs: list[str] = []
    job_id = r["job_id"]
    crow = db.get_contract_row(job_id)
    srow = db.get_state_row(job_id)
    if crow is None or srow is None:
        return [f"DB has no record of {job_id}"]
    if crow["contract_sha256"] != r["contract_sha256"]:
        errs.append("handoff contract SHA-256 differs from approved DB record")
    if srow["current_state"] not in (fsm.ACCEPTED, fsm.HANDOFF_VERIFIED, fsm.CLOSED):
        errs.append(f"DB state of {job_id} is {srow['current_state']}, not ACCEPTED")
    ver = db.query_one("SELECT * FROM qa_verifications WHERE verification_id=?", (r["verification_id"],))
    if ver is None:
        errs.append("verification_id not present in DB")
    else:
        if ver["gate_verdict"] != "ACCEPTED":
            errs.append("DB verification verdict is not ACCEPTED")
        if ver["evidence_manifest_sha256"] != r["evidence_manifest_sha256"]:
            errs.append("evidence manifest SHA-256 differs from DB")
        if ver["attempt_id"] != r["attempt_id"] or ver["job_id"] != job_id:
            errs.append("verification does not belong to handoff attempt/job")
        db_cmds = [(c["command"], c["exit_code"]) for c in json.loads(ver["commands_run_json"])]
        if db_cmds != [(c["command"], c["exit_code"]) for c in r["qa_commands"]]:
            errs.append("QA commands/exit codes differ from DB")
    ev = db.query_one("SELECT job_id FROM audit_events WHERE event_seq=?", (r["last_audit_event_seq"],))
    if ev is None or ev["job_id"] != job_id:
        errs.append("last_audit_event_seq does not reference this job in the audit log")
    return errs


def verify_next_context(next_context_path: str | Path, handoff_path: str | Path) -> VerificationResult:
    npth, hpth = Path(next_context_path), Path(handoff_path)
    if not npth.is_file() or not hpth.is_file():
        return VerificationResult(False, ["next-context or handoff file missing"])
    ntext = npth.read_text(encoding="utf-8")
    hraw = hpth.read_bytes()
    hsha = hashlib.sha256(hraw).hexdigest()
    errors = []
    m = re.search(r"Handoff File SHA-256: `([0-9a-f]{64})`", ntext)
    if not m or m.group(1) != hsha:
        errors.append("NEXT_CONTEXT does not reference the current handoff file hash")
    receipt, err = parse_handoff(hraw.decode("utf-8", "replace"))
    if err:
        errors.append(err)
    else:
        m = re.search(r"Receipt SHA-256: `([0-9a-f]{64})`", ntext)
        if not m or m.group(1) != receipt_digest(receipt):
            errors.append("NEXT_CONTEXT receipt hash mismatch")
        if f"NEXT_CONTEXT {receipt['next_job_id']}" not in ntext:
            errors.append("NEXT_CONTEXT is for a different next job")
    if "UNTRUSTED REFERENCE" not in ntext:
        errors.append("NEXT_CONTEXT lacks untrusted-reference boundary")
    return VerificationResult(not errors, errors, receipt, None, hsha)
