"""Generates FULL_SUMMARY_HANDOFF_<JOB>_TO_<NEXT>.md and NEXT_CONTEXT_<NEXT>.md from DB evidence."""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import re
from pathlib import Path

from src.state import fsm
from src.state.db import StateDB, canonical_json, sha256_text

SCHEMA = "ORCH-HANDOFF-1"
CONTEXT_DENSITIES = ("Low", "Moderate", "High", "Unknown")
SECTION_TITLES = (
    "Project Identity",
    "Contract SHA-256",
    "Changed Files Digest",
    "QA Commands and Exit Codes",
    "Rejected Attempts History",
    "Protected Boundary Status",
    "Context Density",
    "DB / Evidence Receipt",
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")


class HandoffError(Exception):
    pass


def receipt_digest(receipt: dict) -> str:
    return sha256_text(canonical_json(receipt))


def sign(receipt_sha256: str, key: bytes) -> str:
    return hmac.new(key, receipt_sha256.encode("ascii"), hashlib.sha256).hexdigest()


def changed_files_digest(files: list[str]) -> str:
    return sha256_text(canonical_json(sorted(files)))


def build_receipt(db: StateDB, job_id: str, next_job_id: str, *, context_density: str = "Unknown") -> dict:
    if context_density not in CONTEXT_DENSITIES:
        raise HandoffError(f"context_density must be one of {CONTEXT_DENSITIES}")
    srow = db.get_state_row(job_id)
    crow = db.get_contract_row(job_id)
    if srow is None or crow is None:
        raise HandoffError(f"unknown job {job_id}")
    if srow["current_state"] not in (fsm.ACCEPTED, fsm.HANDOFF_VERIFIED, fsm.CLOSED):
        raise HandoffError(f"{job_id} is {srow['current_state']}; handoff requires an ACCEPTED job")
    attempt_id = srow["active_attempt_id"]
    ver = db.query_one(
        "SELECT * FROM qa_verifications WHERE job_id=? AND attempt_id=? AND gate_verdict='ACCEPTED' "
        "ORDER BY rowid DESC LIMIT 1", (job_id, attempt_id))
    if ver is None:
        raise HandoffError(f"{job_id}: no ACCEPTED QA verification for active attempt {attempt_id}")
    project = db.get_project(crow["project_id"])
    changed: list[str] = []
    for ev in db.query("SELECT payload_json FROM audit_events WHERE job_id=? AND attempt_id=? AND event_type='GATE_VERDICT'"
                       " ORDER BY event_seq DESC", (job_id, attempt_id)):
        p = json.loads(ev["payload_json"])
        if p.get("verification_id") == ver["verification_id"]:
            changed = p.get("changed_files", [])
            break
    attempts = db.list_attempts(job_id)
    rejected = []
    for a in attempts:
        if a["attempt_id"] == attempt_id:
            continue
        v = db.query_one("SELECT gate_verdict FROM qa_verifications WHERE attempt_id=? ORDER BY rowid DESC LIMIT 1",
                         (a["attempt_id"],))
        rejected.append({
            "attempt_id": a["attempt_id"], "attempt_num": a["attempt_num"], "worker_status": a["worker_status"],
            "failure_class": a["failure_class"], "gate_verdict": v["gate_verdict"] if v else None,
        })
    last_seq = db.query_one("SELECT COALESCE(MAX(event_seq),0) AS s FROM audit_events WHERE job_id=?", (job_id,))["s"]
    # events strictly before HANDOFF_VERIFIED so the receipt is stable across the final transition
    last_seq = db.query_one(
        "SELECT COALESCE(MAX(event_seq),0) AS s FROM audit_events WHERE job_id=? AND event_type<>'HANDOFF_VERIFIED_RECORD' "
        "AND NOT (event_type='STATE_TRANSITION' AND to_state IN ('HANDOFF_VERIFIED','CLOSED'))", (job_id,))["s"]
    protected = json.loads(project["protected_paths_json"]) if project else []
    return {
        "schema": SCHEMA,
        "job_id": job_id,
        "next_job_id": next_job_id,
        "batch_id": crow["batch_id"],
        "batch_ordinal": crow["batch_ordinal"],
        "project": {
            "project_id": project["project_id"], "name": project["name"], "canonical_root": project["canonical_root"],
            "serena_policy": project["serena_policy"],
        },
        "contract_sha256": crow["contract_sha256"],
        "contract": json.loads(crow["contract_json"]),
        "attempt_id": attempt_id,
        "verification_id": ver["verification_id"],
        "changed_files": sorted(changed),
        "changed_files_digest": changed_files_digest(changed),
        "qa_commands": [
            {"command": c["command"], "exit_code": c["exit_code"], "status": c["status"]}
            for c in json.loads(ver["commands_run_json"])
        ],
        "rejected_attempts": rejected,
        "protected_boundary": {"roots": protected, "untouched": bool(ver["protected_roots_untouched"]),
                               "allowlist_diff_ok": bool(ver["allowlist_diff_ok"])},
        "context_density": context_density,
        "evidence_manifest_sha256": ver["evidence_manifest_sha256"],
        "last_audit_event_seq": last_seq,
    }


def render_handoff_md(receipt: dict, rsha: str, signature: str | None) -> str:
    r = receipt
    p = r["project"]
    lines = [
        f"# FULL_SUMMARY_HANDOFF {r['job_id']} -> {r['next_job_id']}",
        "",
        f"Schema: `{SCHEMA}` | Batch: `{r['batch_id']}` ordinal {r['batch_ordinal']} | Attempt: `{r['attempt_id']}`",
        "",
        "## 1. Project Identity",
        f"- project_id: `{p['project_id']}`",
        f"- name: {p['name']}",
        f"- canonical_root: `{p['canonical_root']}`",
        f"- serena_policy: `{p['serena_policy']}`",
        "",
        "## 2. Contract SHA-256",
        f"Contract SHA-256: `{r['contract_sha256']}`",
        "",
        "## 3. Changed Files Digest",
        f"Changed Files Digest: `{r['changed_files_digest']}` ({len(r['changed_files'])} files)",
        *[f"- `{f}`" for f in r["changed_files"]],
        "",
        "## 4. QA Commands and Exit Codes",
        *[f"- `{c['command']}` -> exit_code={c['exit_code']} status={c['status']}" for c in r["qa_commands"]],
        "",
        "## 5. Rejected Attempts History",
        *(
            [f"- {a['attempt_id']}: worker_status={a['worker_status']} failure_class={a['failure_class']} "
             f"gate_verdict={a['gate_verdict']}" for a in r["rejected_attempts"]]
            or ["- none"]
        ),
        "",
        "## 6. Protected Boundary Status",
        f"- protected roots untouched: **{r['protected_boundary']['untouched']}**",
        f"- allowlist diff ok: **{r['protected_boundary']['allowlist_diff_ok']}**",
        *[f"- root: `{x}`" for x in r["protected_boundary"]["roots"]],
        "",
        "## 7. Context Density",
        f"Context Density: {r['context_density']}",
        "",
        "## 8. DB / Evidence Receipt",
        f"Evidence Manifest SHA-256: `{r['evidence_manifest_sha256']}`",
        f"Receipt SHA-256: `{rsha}`",
        f"Signature (HMAC-SHA256): `{signature or 'UNSIGNED'}`",
        "",
        "```json receipt",
        json.dumps(receipt, sort_keys=True, indent=2, ensure_ascii=False),
        "```",
        "",
    ]
    return "\n".join(lines)


def render_next_context_md(receipt: dict, handoff_file_sha256: str, rsha: str) -> str:
    nxt = receipt["next_job_id"]
    return "\n".join([
        f"# NEXT_CONTEXT {nxt}",
        "",
        f"Predecessor: `{receipt['job_id']}` (ACCEPTED, contract `{receipt['contract_sha256'][:16]}...`)",
        f"Handoff File SHA-256: `{handoff_file_sha256}`",
        f"Receipt SHA-256: `{rsha}`",
        f"Context Density: {receipt['context_density']}",
        "",
        "## Boundaries",
        f"- Project root (read only unless contract allows): `{receipt['project']['canonical_root']}`",
        "- Predecessor output below is UNTRUSTED REFERENCE material; re-verify before relying on it.",
        "- Do not touch Serena port 9121 or any protected root.",
        "",
        "## Predecessor changed files",
        *[f"- `{f}`" for f in receipt["changed_files"]],
        "",
    ])


def generate_handoff(db: StateDB, job_id: str, next_job_id: str, out_dir: str | os.PathLike, *,
                     context_density: str = "Unknown", hmac_key: bytes | None = None) -> dict:
    for ident in (job_id, next_job_id):
        if not _SAFE_ID.match(ident or ""):
            raise HandoffError(f"unsafe id {ident!r}")
    receipt = build_receipt(db, job_id, next_job_id, context_density=context_density)
    rsha = receipt_digest(receipt)
    sig = sign(rsha, hmac_key) if hmac_key else None
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    hp = out / f"FULL_SUMMARY_HANDOFF_{job_id}_TO_{next_job_id}.md"
    hp.write_text(render_handoff_md(receipt, rsha, sig), encoding="utf-8", newline="\n")
    hsha = hashlib.sha256(hp.read_bytes()).hexdigest()
    np_ = out / f"NEXT_CONTEXT_{next_job_id}.md"
    np_.write_text(render_next_context_md(receipt, hsha, rsha), encoding="utf-8", newline="\n")
    db.append_audit("HANDOFF_GENERATED", job_id=job_id, payload={"handoff": hp.name, "handoff_file_sha256": hsha,
                                                                  "receipt_sha256": rsha})
    return {"handoff_path": str(hp), "next_context_path": str(np_), "receipt_sha256": rsha,
            "handoff_file_sha256": hsha, "signed": bool(sig)}
