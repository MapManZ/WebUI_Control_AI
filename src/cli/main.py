"""Unified Orchestra CLI: ``python -m src.cli.main <command>`` (see bin/orchestra.cmd)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _emit(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _supervisor(args):
    from src.orchestra.supervisor import Supervisor

    runtime = Path(args.runtime) if getattr(args, "runtime", None) else REPO_ROOT / ".runtime"
    return Supervisor(REPO_ROOT, runtime_root=runtime)


def _load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _make_worker(name: str):
    if name == "mock":
        from src.workers.mock_worker import MockWorker

        return MockWorker()
    if name.startswith("webui:"):
        from src.workers.webui_packet_worker import WebUIPacketWorker

        return WebUIPacketWorker(name.split(":", 1)[1])
    from src.workers.cli_harness_worker import CLIHarnessWorker

    return CLIHarnessWorker(name)


# ------------------------------------------------------------------ commands
def cmd_init(args) -> int:
    runtime = Path(args.runtime) if args.runtime else REPO_ROOT / ".runtime"
    for d in ("state", "sandboxes", "evidence", "handoffs", "keys", "webui_hub", "webui_packets", "pilot"):
        (runtime / d).mkdir(parents=True, exist_ok=True)
    sup = _supervisor(args)
    try:
        _emit({"ok": True, "runtime": str(runtime), "db": sup.db.path, "journal_mode": sup.db.pragma("journal_mode"),
               "foreign_keys": sup.db.pragma("foreign_keys"), "tables": sup.db.table_names()})
    finally:
        sup.close()
    return 0


def cmd_doctor(args) -> int:
    from src.cli.doctor import print_doctor_report, run_doctor

    report = run_doctor(REPO_ROOT)
    if args.json:
        _emit(report)
    else:
        print_doctor_report(report)
    return 0 if report.get("ok") else 1


def cmd_setup(args) -> int:
    from src.cli.installer import run_semi_auto_setup

    res = run_semi_auto_setup(REPO_ROOT, auto_yes=bool(args.yes))
    if getattr(args, "json", False):
        _emit(res)
    return 0 if res.get("ok") else 1


def cmd_status(args) -> int:
    sup = _supervisor(args)
    try:
        _emit(sup.status())
    finally:
        sup.close()
    return 0


def cmd_submit(args) -> int:
    sup = _supervisor(args)
    try:
        c = _load_json(args.contract)
        r = sup.submit_contract(c, canonical_root=args.root, protected_paths=args.protect or [])
        if args.approve_by:
            r["state"] = sup.approve_contract(c["job_id"], args.approve_by)
        _emit(r)
    finally:
        sup.close()
    return 0


def cmd_run_job(args) -> int:
    sup = _supervisor(args)
    try:
        if args.contract:
            c = _load_json(args.contract)
            if sup.db.get_contract_row(c["job_id"]) is None:
                sup.submit_contract(c, canonical_root=args.root, protected_paths=args.protect or [])
        if args.approve_by:
            if sup.fsm.state(args.job) == "DRAFT":
                sup.approve_contract(args.job, args.approve_by)
        worker = _make_worker(args.worker)
        kwargs = dict(source_root=args.source, include=args.include, worker_mode=args.mode)
        if args.until_done:
            outs = sup.run_until_done(args.job, worker, **kwargs)
        else:
            outs = [sup.run_job_attempt(args.job, worker, **kwargs)]
        _emit([o.to_dict() for o in outs])
        return 0 if outs[-1].accepted else 2
    finally:
        sup.close()


def cmd_verify(args) -> int:
    from src.handoff.verifier import verify_handoff, verify_next_context

    sup = _supervisor(args)
    try:
        res = verify_handoff(args.handoff, db=sup.db if args.against_db else None, hmac_key=sup.hmac_key,
                             require_signature=args.require_signature)
        out = {"ok": res.ok, "errors": res.errors, "receipt_sha256": res.receipt_sha256,
               "handoff_file_sha256": res.handoff_file_sha256, "signed": res.signed}
        if args.next_context:
            n = verify_next_context(args.next_context, args.handoff)
            out["next_context_ok"], out["next_context_errors"] = n.ok, n.errors
            res_ok = res.ok and n.ok
        else:
            res_ok = res.ok
        if args.reconstruct and res_ok:
            out["reconstruction"] = sup.reconstruct_from_handoff(args.handoff, next_context_path=args.next_context)
        _emit(out)
        return 0 if res_ok else 1
    finally:
        sup.close()


def cmd_handoff(args) -> int:
    sup = _supervisor(args)
    try:
        _emit(sup.finalize_handoff(args.job, args.next, context_density=args.density))
    finally:
        sup.close()
    return 0


def cmd_reconcile(args) -> int:
    sup = _supervisor(args)
    try:
        rep = sup.reconcile_crashed_jobs(assume_all_stale=args.assume_all_stale)
        _emit([f.__dict__ for f in rep.findings])
    finally:
        sup.close()
    return 0


def cmd_pilot(args) -> int:
    from src.integrations.rea_pilot import run_pilot

    res = run_pilot(REPO_ROOT, deep=args.deep)
    _emit({k: res[k] for k in ("go_no_go", "protected_tree_untouched", "g7_two_job_gate_enforced", "job_states",
                               "handoff_reconstruction_ok", "fingerprint_mode")})
    return 0 if res["go_no_go"] == "GO" else 1


def cmd_webui_dispatch(args) -> int:
    from src.workers.webui_packet_worker import WebUIPacketWorker

    contract = _load_json(args.contract) if args.contract else {"job_id": args.job or "WEBUI-JOB", "goal": args.goal or "analysis"}
    job_id = str(contract.get("job_id", args.job or "WEBUI-JOB"))
    out_dir = (Path(args.runtime) if args.runtime else REPO_ROOT / ".runtime") / "webui_packets" / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    res = WebUIPacketWorker(args.provider).start(contract, str(out_dir), f"{job_id}-DISPATCH", context_id=args.context)
    _emit({"status": res.status, "provider": args.provider, "packet_sha256": res.metadata.get("packet_sha256"),
           "files": [str(out_dir / f) for f in res.changed_files],
           "next": "paste the prompt into the WebUI, then submit the reply via the dashboard or verify_and_import_response"})
    return 0


def cmd_webui_probe(args) -> int:
    cfg_path = REPO_ROOT / "config" / "browser_targets.example.json"
    report: dict = {"config": str(cfg_path), "providers": {}}
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        cfg = {}
        report["config_error"] = str(exc)
    targets = cfg.get("targets", cfg.get("providers", cfg)) if isinstance(cfg, dict) else {}
    names = [args.provider] if args.provider else ["chatgpt", "claude", "gemini", "glm", "deepseek"]
    for n in names:
        t = targets.get(n) if isinstance(targets, dict) else None
        report["providers"][n] = {"configured": bool(t), "url": (t or {}).get("url") if isinstance(t, dict) else None}
    if args.cdp_port:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{int(args.cdp_port)}/json/version", timeout=2) as r:
                report["cdp"] = {"port": args.cdp_port, "reachable": True, "browser": json.loads(r.read()).get("Browser")}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            report["cdp"] = {"port": args.cdp_port, "reachable": False, "error": str(exc)}
    if args.harness:
        from src.workers.cli_harness_worker import probe_harness

        report["harness"] = probe_harness(args.harness)
    _emit(report)
    return 0 if all(v["configured"] for v in report["providers"].values()) else 1


def cmd_dashboard(args) -> int:
    from src.webui.dashboard_server import DashboardServer

    server = DashboardServer(port=args.port)
    if args.smoke:
        server.start_background()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/api/health", timeout=5) as r:
                _emit({"ok": r.status == 200, "port": server.port, "body": json.loads(r.read())})
        finally:
            server.shutdown()
        return 0
    print(f"Dashboard at http://127.0.0.1:{server.port}/  (Ctrl+C to stop)")
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orchestra", description="Open multi-orchestra control CLI")
    p.add_argument("--runtime", help="runtime dir (default <repo>/.runtime)")
    sp = p.add_subparsers(dest="cmd", required=True)

    sp.add_parser("init", help="create runtime dirs + SQLite WAL DB").set_defaults(fn=cmd_init)
    su = sp.add_parser("setup", help="run semi-automated setup wizard (5 steps)")
    su.add_argument("--yes", "-y", action="store_true", help="auto-confirm all setup steps")
    su.add_argument("--json", action="store_true", help="emit JSON setup report after summary")
    su.set_defaults(fn=cmd_setup)
    d = sp.add_parser("doctor", help="probe local tools / harnesses / WebUIs")
    d.add_argument("--json", action="store_true")
    d.set_defaults(fn=cmd_doctor)
    sp.add_parser("status", help="job + DB status").set_defaults(fn=cmd_status)

    s = sp.add_parser("submit", help="register a contract (optionally approve)")
    s.add_argument("--contract", required=True)
    s.add_argument("--root")
    s.add_argument("--protect", action="append")
    s.add_argument("--approve-by")
    s.set_defaults(fn=cmd_submit)

    r = sp.add_parser("run-job", help="run one attempt (or the bounded repair loop) for a job")
    r.add_argument("--job", required=True)
    r.add_argument("--contract")
    r.add_argument("--root")
    r.add_argument("--protect", action="append")
    r.add_argument("--approve-by")
    r.add_argument("--worker", default="mock", help="mock | antigravity | opencode | hermes | dsh | codex | webui:<provider>")
    r.add_argument("--mode", help="mock worker behaviour")
    r.add_argument("--source", help="source dir snapshotted into the sandbox")
    r.add_argument("--include", action="append", help="glob(s) selecting files to snapshot")
    r.add_argument("--until-done", action="store_true")
    r.set_defaults(fn=cmd_run_job)

    v = sp.add_parser("verify", help="machine-verify a handoff file")
    v.add_argument("--handoff", required=True)
    v.add_argument("--next-context")
    v.add_argument("--against-db", action="store_true")
    v.add_argument("--require-signature", action="store_true")
    v.add_argument("--reconstruct", action="store_true")
    v.set_defaults(fn=cmd_verify)

    h = sp.add_parser("handoff", help="generate + verify handoff for an ACCEPTED job")
    h.add_argument("--job", required=True)
    h.add_argument("--next", required=True)
    h.add_argument("--density", default="Unknown", choices=["Low", "Moderate", "High", "Unknown"])
    h.set_defaults(fn=cmd_handoff)

    rc = sp.add_parser("reconcile", help="pause interrupted jobs (never auto-accept)")
    rc.add_argument("--assume-all-stale", action="store_true")
    rc.set_defaults(fn=cmd_reconcile)

    pl = sp.add_parser("pilot", help="isolated REA pilot (read-only on SummoningRemake)")
    pl.add_argument("--deep", action="store_true", help="content-hash the whole REA tree (slow)")
    pl.set_defaults(fn=cmd_pilot)

    wd = sp.add_parser("webui-dispatch", help="create a WebUI work packet + prompt")
    wd.add_argument("--provider", required=True, choices=["chatgpt", "claude", "gemini", "glm", "deepseek"])
    wd.add_argument("--contract")
    wd.add_argument("--job")
    wd.add_argument("--goal")
    wd.add_argument("--context", default="B")
    wd.set_defaults(fn=cmd_webui_dispatch)

    wp = sp.add_parser("webui-probe", help="check WebUI target config / CDP / CLI harness")
    wp.add_argument("--provider")
    wp.add_argument("--cdp-port", type=int)
    wp.add_argument("--harness")
    wp.set_defaults(fn=cmd_webui_probe)

    db = sp.add_parser("dashboard", help="serve the local dashboard (127.0.0.1)")
    db.add_argument("--port", type=int, default=8765)
    db.add_argument("--smoke", action="store_true", help="start, GET /api/health, stop")
    db.set_defaults(fn=cmd_dashboard)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except (ImportError, FileNotFoundError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 3
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
