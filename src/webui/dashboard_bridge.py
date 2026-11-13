"""CLI bridge for interacting with the WebUI Control AI Dashboard server."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

JOB_RE = re.compile(r"^[0-9a-zA-Z_-]{8,64}$")
ROLES = ("B", "C", "D")


def request(port: int, path: str, data: Optional[Dict[str, Any]] = None) -> Any:
    if not 1024 <= port <= 65535:
        raise ValueError("Invalid localhost port")
    root = f"http://127.0.0.1:{port}"
    headers = {"Content-Type": "application/json; charset=utf-8"} if data is not None else {}
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    req = urllib.request.Request(
        root + path,
        headers=headers,
        data=body,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Dashboard HTTP {exc.code}: {detail}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="WebUI Control AI Dashboard Bridge CLI")
    parser.add_argument("--port", type=int, default=8765, help="Dashboard port (default: 8765)")
    sub = parser.add_subparsers(dest="operation", required=True)

    # fetch
    p_fetch = sub.add_parser("fetch", help="Fetch role prompt for a job")
    p_fetch.add_argument("--job", required=True, help="Job identifier")
    p_fetch.add_argument("--role", default="B", help="Role / context ID (B, C, D)")

    # status
    p_status = sub.add_parser("status", help="Get status of a job")
    p_status.add_argument("--job", required=True, help="Job identifier")

    # submit
    p_sub = sub.add_parser("submit", help="Submit a worker JSON response file")
    p_sub.add_argument("--job", required=True, help="Job identifier")
    p_sub.add_argument("--response-file", type=Path, required=True, help="Path to JSON response file")

    # registry
    p_reg = sub.add_parser("registry", help="Query the shared reference registry")
    p_reg.add_argument("--role", choices=ROLES, default=None, help="Filter by role")
    p_reg.add_argument("--project", default="shared", help="Project scope")
    p_reg.add_argument("--kind", default=None, help="Filter by kind (skill, tool, hook, knowledge)")

    args = parser.parse_args()

    if args.operation == "registry":
        params = {}
        if args.project:
            params["project"] = args.project
        if args.role:
            params["role"] = args.role
        if args.kind:
            params["kind"] = args.kind
        query_str = ("?" + urlencode(params)) if params else ""
        try:
            catalog = request(args.port, f"/api/registry{query_str}")
            print(json.dumps(catalog, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(f"REGISTRY_READ_FAILED: {exc}", file=sys.stderr)
            return 2

    job_id = args.job
    if not JOB_RE.fullmatch(job_id):
        print(f"ERROR: Invalid job ID '{job_id}'", file=sys.stderr)
        return 2

    try:
        if args.operation == "fetch":
            job = request(args.port, f"/api/jobs/{job_id}")
            prompts = job.get("prompts", {})
            if args.role not in prompts:
                print(f"ERROR: Role '{args.role}' prompt not found in job. Available: {list(prompts.keys())}", file=sys.stderr)
                return 2
            print(prompts[args.role])
            return 0

        if args.operation == "status":
            job = request(args.port, f"/api/jobs/{job_id}")
            print(json.dumps({
                "job_id": job.get("job_id"),
                "case_id": job.get("case_id"),
                "status": job.get("status"),
                "missing": job.get("missing"),
                "slots": job.get("slots"),
            }, ensure_ascii=False, indent=2))
            return 0

        if args.operation == "submit":
            if not args.response_file.is_file():
                print(f"ERROR: Response file not found: {args.response_file}", file=sys.stderr)
                return 2
            data = json.loads(args.response_file.read_text(encoding="utf-8"))
            result = request(args.port, f"/api/jobs/{job_id}/submit", {"response": data})
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

    except Exception as exc:
        print(f"BRIDGE_OPERATION_FAILED: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
