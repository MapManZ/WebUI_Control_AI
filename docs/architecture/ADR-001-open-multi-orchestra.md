# ADR-001: Open Multi-Engine Autonomous Orchestra Architecture

## Status
**Accepted** (2026-11-13)

## Context
Modern software development environments frequently utilize a heterogeneous mix of artificial intelligence engines:
1. **Web-Based Conversational Engines**: Claude (Anthropic), ChatGPT (OpenAI), Gemini (Google), GLM (Zhipu AI), and DeepSeek Chat.
2. **Local Desktop & CLI Coding Agents**:
   - Google Antigravity (`agy`)
   - OpenCode CLI (`opencode-cli`)
   - Nous Research Hermes Agent (`hermes`)
   - DeepSeek Harness (`DeepSeek Harness.exe`)
3. **Dedicated High-Value Codebases**: Critical projects such as `O:\AI_Workspace\REA Project\OpenCodeREA\SummoningRemake` with existing governance agreements and active background services (e.g., Serena MCP on port 9121).

Prior orchestration approaches were tightly coupled to single proprietary toolkits or required bloated Python virtual environments with hundreds of third-party pip dependencies that break across minor interpreter updates. Furthermore, lack of strict contract boundaries allowed rogue agents to write outside their sandboxes or cascade failures across multiple steps.

## Decision Drivers
- **Zero Installation Friction**: Must operate seamlessly on Python 3.14 with zero external pip dependencies.
- **Strict Boundary Safety**: Must guarantee that high-value external projects (REA Project) and designated network ports (Serena MCP port 9121) are never mutated or interrupted.
- **Universal Interoperability**: Must support any model or tool as either the Orchestrator or the Worker.
- **Deterministic Governance**: Every job must adhere to an immutable, cryptographically hashed contract (`JobContract`).
- **Fail-Fast Batch Control**: Prevent error cascading by restricting batch progression through the Two-Job Gate (`G7`).

## Architectural Decisions

### 1. Zero External Pip Dependencies (Python 3.14 Stdlib)
The entire orchestra runtime, contract validator, state machine, sandbox manager, and CLI diagnostics are implemented strictly using the Python 3.14 standard library (`json`, `hashlib`, `pathlib`, `subprocess`, `sqlite3`, `http.server`, `urllib`, `dataclasses`, `typing`, `unittest`).

### 2. Anyone-as-Orchestrator Pattern
The orchestration core does not care whether the primary coordinator is a human typing in a terminal, an Antigravity subagent via stdio MCP, an OpenCode task, or a web dashboard. The engine exposes three parity interfaces:
- **CLI**: `bin/orchestra.cmd`
- **MCP Server**: Stdio JSON-RPC protocol (`src/mcp/orchestra_mcp.py`)
- **Web Dashboard**: Embedded HTTP server (`src/webui/dashboard_server.py`)

### 3. Open Pluggable CLI Harness Registry (`config/harnesses.json`)
Local CLI agents are abstracted behind a unified configuration registry. Each harness defines its executable path, Windows CMD wrapper, argument templates, profiles, and supported protocols (`cli`, `acp`, `mcp`).

Special accommodation is made for **DeepSeek Harness**, which is distributed as an Electron package on Windows. By setting `ELECTRON_RUN_AS_NODE=1` and evaluating an internal loader script, `bin/dsh-cli.cmd` unlocks full headless CLI execution without requiring GUI interaction.

### 4. Cryptographic Job Contracts & DAG Validation
Every operational unit is declared as a `JobContract` complying with `contracts/job_contract.schema.json`.
- Each contract is sealed with a canonical SHA-256 digest (`contract_sha256`) computed over all fields except the hash itself.
- Write paths are strictly relative; any attempt to specify parent traversals (`..`), absolute paths, Windows drive letters (`C:`), or UNC shares is rejected during contract validation.
- Backlogs are verified as directed acyclic graphs (DAGs), rejecting cycles and dangling dependencies.

### 5. Two-Job Batch Gate (G7)
To prevent error cascading:
- Backlogs are divided into batches of **at most 2 sequential jobs**.
- Job 2 of a batch cannot execute until Job 1 has passed all deterministic acceptance checks and produced a verified handoff artifact (`FULL_SUMMARY_HANDOFF_<JOB>_TO_<NEXT>.md`).
- If a job fails, it enters a structured repair loop bounded by `max_rework_cycles` (default 3) and `max_infra_retries` (default 2). Any scope violation immediately transitions the job to `BLOCKED`.

### 6. Sandbox Isolation and Protected Root Governance
Workers execute exclusively inside isolated disposable sandboxes (`.runtime/sandboxes/<JOB_ID>/`).
External repositories (e.g. `O:\AI_Workspace\REA Project\OpenCodeREA\SummoningRemake`) are mounted strictly read-only or snapshotted into disposable pilot directories. Pre- and post-execution SHA-256 tree hashing proves zero mutations in protected boundaries.

## Consequences

### Positive
- **Rock-solid determinism**: Immune to virtualenv corruption, package deprecations, or network pip outages.
- **Safety**: Impossible for a worker agent to write outside its approved sandbox without failing the contract validator.
- **Transparency**: Every job transition, output diff, and acceptance check is stored in an auditable SQLite database and reproducible markdown handoff files.

### Negative / Trade-offs
- Advanced HTTP routing or schema validations that are conventionally offloaded to heavyweight third-party packages (e.g., `pydantic`, `fastapi`) must be implemented cleanly using standard library mechanisms.
