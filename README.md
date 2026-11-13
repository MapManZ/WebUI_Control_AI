# WebUI Control AI: Open Multi-Engine Autonomous Orchestra

[![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)](https://www.python.org/downloads/)
[![Zero External Dependencies](https://img.shields.io/badge/dependencies-zero%20pip-brightgreen.svg)]()
[![Platform: Windows](https://img.shields.io/badge/platform-windows-lightgrey.svg)]()
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An open, vendor-neutral, multi-engine autonomous orchestration platform designed to seamlessly integrate both browser-based AI WebUIs (Claude, ChatGPT, Gemini, GLM, DeepSeek) and local CLI coding harnesses (Antigravity, OpenCode, Hermes, DeepSeek Harness).

Built with **100% Python 3.14 standard library** (zero external pip dependencies) for maximum reliability, speed, and determinism.

> [!TIP]
> 📖 **คู่มือภาษาไทยสำหรับผู้เริ่มต้น (Beginner Guide)**:
> - **[คู่มือแบบภาพประกอบ Step-by-Step (docs/VISUAL_STEP_BY_STEP_GUIDE_TH.md)](docs/VISUAL_STEP_BY_STEP_GUIDE_TH.md)** — อธิบายทุกจุดบนหน้าจอ Dashboard พร้อมภาพประกอบ, Working Directory, MCP (Lnwjud) และ Settings
> - **[คู่มือสอนทีละก้าวสำหรับมือใหม่ (docs/BEGINNER_STEP_BY_STEP_TH.md)](docs/BEGINNER_STEP_BY_STEP_TH.md)** — ติดตั้งง่ายในคลิกเดียว สอนการใช้งาน Dashboard, CLI และ MCP ทีละขั้น
> - **[คู่มือฉบับสมบูรณ์ (docs/USER_MANUAL_TH.md)](docs/USER_MANUAL_TH.md)** — อธิบายสถาปัตยกรรม, กฎ 7 ขั้นตอน, และการจัดการสัญญาละเอียดทุกแง่มุม

---

## Key Capabilities

1. **Semi-Automated Setup & 1-Click Launchers**:
   - Double-click `INSTALL_SETUP.cmd` to run the 5-step automated wizard (Doctor preflight -> SQLite WAL DB -> Browser configs -> Ready-to-use MCP configs -> CLI self-test).
   - Double-click `START_DASHBOARD.cmd` to immediately launch and open the interactive web control room (`http://127.0.0.1:8765`).
2. **Model Role Policy (4 Tiers)**:
   - 🟢 **Gemini 3.8 Flash** (`gemini-3.8-flash`): Default Worker for high-speed general coding, unit testing, and script runs.
   - 🔵 **Gemini 4 Hydrogen** (`gemini-4-hydrogen`): Advanced Worker for multi-step reasoning, architectural implementation, and deep bug hunting.
   - 🟠 **Claude Sonnet 5.5** (`claude-sonnet-5.5`): Hard Tasks Worker reserved for complex architecture and high-security components.
   - 🔴 **Gemini 4 Argon** (`gemini-4-argon`): Reviewer & Merge-Only gate (`allow_worker_execution: false`). Reserved strictly for evaluating QA criteria and merging code; blocked by policy from acting as a coding worker.
3. **Pluggable CLI Harnesses (4 Local Agents)**:
   - [Antigravity](bin/agy-cli.cmd) (`bin/agy-cli.cmd`)
   - [OpenCode Desktop](bin/opencode-cli.cmd) (`bin/opencode-cli.cmd`)
   - [Hermes Agent](bin/hermes-cli.cmd) (`bin/hermes-cli.cmd`)
   - [DeepSeek Harness](bin/dsh-cli.cmd) (`bin/dsh-cli.cmd`) — unlocked as headless CLI via Electron wrapper.
4. **WebUI Provider Support (5 Browsers)**:
   - Config-driven browser automation and clipboard packet generation for `Claude`, `ChatGPT`, `Gemini`, `GLM` (Z.ai / ChatGLM), and `DeepSeek`.
5. **Deterministic Contract Governance**:
   - Rigid contract schemas ([job_contract.schema.json](contracts/job_contract.schema.json)) with SHA-256 integrity validation, path traversal prevention, and DAG cycle detection.
6. **Two-Job Batch Gate (G7)**:
   - Limits batches to at most 2 sequential jobs, preventing runaway state drift and requiring verified QA handoff before batch progression.
7. **Strict Workspace Protection**:
   - Zero-touch boundary protection for external repositories (configurable via `PROTECTED_ABS_ROOTS` and port boundaries).

---

## Directory Structure

```
WebUI_Control_AI/
├── INSTALL_SETUP.cmd                # 1-Click Semi-Automated Installer (Wizard)
├── START_DASHBOARD.cmd              # 1-Click Dashboard Launcher (port 8765)
├── bin/                             # Windows executable wrappers
│   ├── agy-cli.cmd                  # Antigravity CLI wrapper
│   ├── dsh-cli.cmd                  # DeepSeek Harness CLI unlock wrapper
│   ├── hermes-cli.cmd               # Hermes Agent CLI wrapper
│   ├── opencode-cli.cmd             # OpenCode CLI wrapper
│   └── orchestra.cmd                # Unified Orchestra CLI runner
├── config/
│   ├── browser_targets.example.json # 5 WebUI targets and selectors
│   ├── browser_targets.json         # Active WebUI configuration (auto-generated)
│   └── harnesses.json               # Pluggable CLI harness & Model policy registry
├── contracts/
│   ├── job_contract.schema.json     # JSON Schema for Job Contracts
│   └── REA_ORCHESTRA_JOB_BACKLOG_V2.json # Backlog of jobs (ORCH-001..ORCH-501)
├── docs/
│   ├── BEGINNER_STEP_BY_STEP_TH.md  # Beginner step-by-step Thai guide
│   ├── USER_MANUAL_TH.md            # Comprehensive Thai user manual
│   └── architecture/
│       ├── ADR-001-open-multi-orchestra.md
│       └── DISCOVERY_INVENTORY_2026-11-13.json
├── src/
│   ├── cli/
│   │   ├── __init__.py
│   │   ├── doctor.py                # Environment & harness health diagnostics
│   │   ├── installer.py             # Semi-automated setup logic
│   │   └── main.py                  # CLI entrypoint (doctor, setup, webui-dispatch, etc.)
│   ├── contracts/
│   │   ├── __init__.py
│   │   ├── models.py                # JobContract dataclass
│   │   └── validator.py             # Contract & DAG validators with SHA-256
│   ├── mcp/
│   │   └── orchestra_mcp.py         # Stdio JSON-RPC MCP Server
│   ├── state/
│   │   └── db.py                    # SQLite WAL database manager
│   ├── webui/
│   │   ├── dashboard_server.py      # HTTP Dashboard server with Model selection
│   │   └── static/                  # Web Dashboard HTML/JS/CSS assets
│   └── workers/
│       └── cli_harness_worker.py    # Multi-engine CLI runner with Model policy gate
└── tests/
    ├── __init__.py
    ├── test_contracts.py            # Contract model and DAG validation tests
    ├── test_doctor.py               # Preflight doctor unit tests
    └── test_installer_and_models.py # Installer, Model policy, & Hydrogen tests
```

---

## Quickstart

### 1. Semi-Automated Setup (Recommended)
Double-click `INSTALL_SETUP.cmd` or run in terminal:
```cmd
.\INSTALL_SETUP.cmd
```
For non-interactive / CI automation:
```cmd
.\INSTALL_SETUP.cmd --yes
```

### 2. Launch Interactive Web Dashboard
Double-click `START_DASHBOARD.cmd` (opens `http://127.0.0.1:8765` in your default browser).

### 3. Preflight Diagnostics
Run the preflight health check anytime to verify all local runtimes, models, and CLI harnesses:
```cmd
.\bin\orchestra.cmd doctor
```

### 4. Run DeepSeek Harness Headless
The repository includes an Electron wrapper that unlocks DeepSeek Harness as a standalone CLI on Windows:
```cmd
.\bin\dsh-cli.cmd --version
.\bin\dsh-cli.cmd --profile headless --help
```

### 5. Run Test Suite
Run the full unit and integration test suite:
```cmd
python -m unittest discover -s tests -p "test_*.py" -v
```

---

## Documentation
- [Beginner Step-by-Step Guide (คู่มือสำหรับมือใหม่แบบจับมือทำ)](docs/BEGINNER_STEP_BY_STEP_TH.md)
- [Thai User Manual (คู่มือการใช้งานฉบับสมบูรณ์ภาษาไทย)](docs/USER_MANUAL_TH.md)
- [Architecture Decision Record (ADR-001)](docs/architecture/ADR-001-open-multi-orchestra.md)
- [Discovery Tool Inventory](docs/architecture/DISCOVERY_INVENTORY_2026-11-13.json)
