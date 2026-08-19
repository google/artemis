# 🔌 Universal MCP Server for ARTEMIS

This directory contains the universal **Model Context Protocol (MCP)** server for **ARTEMIS**, enabling seamless mobile device automation inside AI IDEs and agents like **Antigravity**, **Cursor**, **Claude Code**, **OpenClaw**, and **Windsurf**.

## 🏗️ Architecture

```
mcp_server/
├── __init__.py           # Package exports (mcp, main)
├── base.py               # Shared FastMCP instance
├── server.py             # Server entrypoint (stdio / sse)
├── rules.md              # AI Agent Testing Mindset & system prompt rules for IDEs
├── background/           # Background process execution
│   └── task_runner.py    # Subprocess runner for Artemis Agent
├── notifiers/            # Multi-environment notification adapters
│   ├── base.py           # BaseNotifier abstract class
│   ├── agentapi.py       # Jetski / Antigravity AgentAPI notifier
│   ├── webhook.py        # OpenClaw / CI / Discord / Slack webhook notifier
│   ├── desktop.py        # Native OS toast notifier
│   ├── file.py           # File audit logger
│   └── composite.py      # Multi-channel notification dispatcher
├── tools/                # Modular standard MCP tools
│   ├── task_runner.py    # mobile_run_task
│   ├── task_manager.py   # mobile_manage_task
│   ├── device_state.py   # mobile_get_device_state
│   └── inspect_trace.py  # mobile_inspect_trace
└── utils/                # Environment, device, and trace utilities
    ├── device_utils.py   # Cross-platform ADB and emulator resolver
    ├── env_utils.py      # Python interpreter and process manager
    └── trace_store.py    # Traces directory and status manager
```

## 🧠 AI Agent Behavioral Rules (`rules.md`)

When connecting ARTEMIS to AI coding assistants like **Antigravity**, **Claude Code**, **Cursor**, or **Windsurf**, providing the assistant with domain-specific testing discipline is critical for generating reliable test code.

The included [`rules.md`](./rules.md) file contains the **Mobile Testing Mindset (ARTEMIS Integration)** guideline. It teaches the AI agent how to properly collaborate with ARTEMIS:

1. **The Runnable Code Principle & Active Exploration**: Instructs the AI to never guess or hallucinate UI transitions. Instead, it must first explore the live app via ARTEMIS MCP tools (`mobile_run_task`, etc.) to discover and verify real-world interaction paths before writing test scripts.
2. **Flash vs. Pro Routing Strategy**: Guides the AI to choose **Flash** for rapid, straightforward UI actions (< 30 steps) and **Pro** for complex multi-agent planning, polling loops, or video/log diagnosis.
3. **Latency & Timing Compensation**: Clarifies the difference between AI exploratory latency (e.g., model decision intervals) and the deterministic timing requirements of final test code.
4. **"Dynamic-First, Coordinate-Fallback" Locator Pattern**: Teaches the AI to prioritize dynamic UI locators (Resource IDs, OCR text, semantics) for layout resilience, while implementing absolute coordinate fallbacks for maximum execution reliability.

### How to Apply `rules.md` in Your IDE
* **Antigravity**: Add or copy the contents of `rules.md` into your Workspace Rules, Global Rules settings, or agent instructions.
* **Claude Code**: Copy or include the contents of `rules.md` in your project's `CLAUDE.md` file (or reference it directly in your instructions).
* **Cursor**: Copy the contents of `rules.md` into your `.cursorrules` or create a new rule file in `.cursor/rules/artemis.mdc`.
* **Windsurf / OpenClaw**: Add the rules to your global/workspace rules or agent system prompts so the assistant always follows verified mobile testing principles.

## 🛠️ MCP Tools Overview

* **`mobile_run_task`**: Asynchronously launches an autonomous mobile automation task (`Flash` or `Pro` model).
* **`mobile_manage_task`**: Manages task lifecycle (`status`, `stop`, `inject_instruction`).
* **`mobile_get_device_state`**: Real-time observer (`screenshot` or OCR+XML `hierarchy`).
* **`mobile_inspect_trace`**: Granular trace inspection, visual action overlays, and agent reasoning.

## 🚀 How to Run & Configure

### CLI Execution
```bash
# Start server over stdio
python -m mcp_server

# Or via Artemis CLI
artemis mcp
```

### IDE Configuration

#### One-Click Auto Install (Recommended)
Run the automated installer to detect and configure your IDE (Antigravity, Cursor, Claude Code/Desktop, OpenClaw):
```bash
artemis mcp --install all
# Or install specifically for Antigravity:
artemis mcp --install antigravity
```

#### Manual JSON Config Generation
Run `artemis mcp --generate-config antigravity` (or `all`) to output ready-to-use configuration JSON with resolved `.venv` Python and project paths.
