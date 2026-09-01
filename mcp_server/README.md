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
│   ├── agentapi.py       # Jetski / Antigravity AgentAPI notifier (with session sorting & caching)
│   ├── desktop.py        # Native OS toast notifier (macOS/Linux/Windows, enabled by default)
│   ├── script.py         # Universal Script / Command Hook notifier (ARTEMIS_NOTIFY_CMD)
│   ├── webhook.py        # OpenClaw / CI / Discord / Slack webhook notifier
│   ├── file.py           # File audit logger (notifications.jsonl)
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

## 🔔 Multi-Platform Notification & Wakeup Support

When you start an asynchronous mobile test via `mobile_run_task`, Artemis MCP runs the job as a detached background process so the AI agent is not blocked. Upon completion or failure, Artemis dispatches alerts across **all available notification channels** so that both human developers and AI agents are notified immediately across different platforms:

| Platform / Environment | Primary Notification Mechanism | How It Works & Configuration |
| :--- | :--- | :--- |
| **Antigravity / Jetski** | **Reactive Wakeup (`AgentApiNotifier`)** | Automatically detects and caches the active IDE session credentials (`ANTIGRAVITY_LS_ADDRESS`, `ANTIGRAVITY_CSRF_TOKEN`), sorting candidate processes by creation time to avoid stale PIDs, and calls `agentapi send-message` to wake up the agent directly in the conversation. |
| **Cursor / Windsurf / VS Code / Cline / Roo Code** | **Native Desktop Toast (`DesktopNotifier`) + File Audit (`FileNotifier`)** | Enabled **by default** across macOS (`osascript`), Linux (`notify-send`), and Windows (`powershell`). Pops up a system notification banner informing the developer whether the task completed or failed. The AI agent inspects `<trace_dir>/status.json` or uses `mobile_manage_task(action="status")`. Can be silenced via `ARTEMIS_DESKTOP_NOTIFY=false`. |
| **Claude Code / Desktop** | **Native Desktop Toast + File Audit** | Alerts the user via system desktop notification banner when background execution concludes, while maintaining a complete JSONL audit log in `notifications.jsonl`. |
| **OpenClaw / Slack / Discord / CI/CD** | **HTTP Webhook (`WebhookNotifier`)** | Sends structured JSON POST payloads to custom endpoints configured via `OPENCLAW_WEBHOOK_URL`, `MCP_NOTIFICATION_WEBHOOK`, or `ARTEMIS_WEBHOOK_URL`. |
| **Universal Custom IDE / CLI Hooks** | **Custom Script Hook (`ScriptNotifier`)** | Set `ARTEMIS_NOTIFY_CMD="my-script --title '{title}' --message '{message}' --trace-id '{trace_id}'"` in your environment to execute any custom command or script upon event completion. |

## 🧠 AI Agent Behavioral Rules (`rules.md`)

When connecting ARTEMIS to AI coding assistants like **Antigravity**, **Claude Code**, **Cursor**, or **Windsurf**, providing the assistant with domain-specific testing discipline is critical for generating reliable test code.

The included [`rules.md`](./rules.md) file contains the **Mobile Testing Mindset (ARTEMIS Integration)** guideline. It teaches the AI agent how to properly collaborate with ARTEMIS:

1. **The Runnable Code Principle & Active Exploration**: Instructs the AI to never guess or hallucinate UI transitions. Instead, it must first explore the live app via ARTEMIS MCP tools (`mobile_run_task`, etc.) to discover and verify real-world interaction paths before writing test scripts.
2. **Flash vs. Pro Routing Strategy**: Guides the AI to choose **Flash** for rapid, straightforward UI actions (< 30 steps) and **Pro** for complex multi-agent planning, polling loops, or video/log diagnosis.
3. **Latency & Timing Compensation**: Clarifies the difference between AI exploratory latency (e.g., model decision intervals) and the deterministic timing requirements of final test code.
4. **"Dynamic-First, Coordinate-Fallback" Locator Pattern**: Teaches the AI to prioritize dynamic UI locators (Resource IDs, OCR text, semantics) for layout resilience, while implementing absolute coordinate fallbacks for maximum execution reliability.

### How Global Rules & MCP are Installed
When you run `uv run artemis mcp --install all` (or target a specific IDE like `--install cursor`, `--install claude`, etc.), the installer **automatically synchronizes both global MCP server configuration and global testing rules** into the corresponding user home directories (without creating project-level rule files in your workspace):
* **Antigravity / Jetski**: Automatically installs global rules to `~/.gemini/rules/artemis.md`.
* **Cursor**: Automatically installs global YAML-frontmatter rule file at `~/.cursor/rules/artemis.mdc`.
* **Claude Code / Desktop**: Automatically installs global rules to `~/.claude/rules/artemis.md`.
* **Windsurf**: Automatically installs global rules to `~/.codeium/windsurf/rules/artemis.md`.
* **VS Code**: Automatically installs global rules to `~/.vscode/rules/artemis.md`.
* **Cline / Roo Code**: Automatically installs global rules to `~/.cline/rules/artemis.md` and `~/.roo/rules/artemis.md`.
* **OpenClaw**: Automatically installs global rules to `~/.openclaw/rules/artemis.md`.

*(Note: If you prefer manual configuration, you can also copy the contents of [`rules.md`](./rules.md) into your IDE's system prompt or global rules settings.)*

## 🛠️ MCP Tools Overview

* **`mobile_run_task`**: Asynchronously launches an autonomous mobile automation task (`Flash` or `Pro` model) with optional `device_serial` targeting.
* **`mobile_manage_task`**: Manages task lifecycle (`status`, `stop`, `inject_instruction`), returning task state and assigned `device_serial`. Pass `release_loop=True` with `inject_instruction` to gracefully end a `[Loop:continuous]` monitoring task — this explicit signal (not "please stop" wording) is what unlocks the loop milestone's completion.
* **`mobile_get_device_state`**: Real-time observer (`screenshot` or OCR+XML `hierarchy`) with optional `device_serial`.
* **`mobile_inspect_trace`**: Granular trace inspection, visual action overlays, agent reasoning, and `device_serial` tracking.

### 📱 Device Selection & Multi-Device Execution
ARTEMIS supports parallel execution across multiple connected Android devices and emulators:
1. **Direct Device Specification**: Provide `device_serial` explicitly (e.g. `device_serial="63191FDKX00062"` or `device_serial="emulator-5554"`). Per-device execution locks allow distinct devices to run in parallel.
2. **Automatic Device Selection**: Omit `device_serial` to let ARTEMIS auto-select an available ready device from the device pool.
* **User Choice Priority**: When multiple devices/emulators are connected, AI agents should prioritize asking the user which device to run on.
* **Device Diagnosis**: Inspect connected hardware, authorization status, and serials anytime via `adb devices` or `adb devices -l`.

## 🚀 How to Run & Configure

### CLI Execution
```bash
# Start server over stdio
python -m mcp_server

# Or via Artemis CLI
uv run artemis mcp
```

### IDE Configuration

#### One-Click Auto Install (Recommended)
Run the automated installer to detect and configure your IDE (Antigravity, Cursor, Claude Code/Desktop, OpenClaw):
```bash
uv run artemis mcp --install all
# Or install specifically for Antigravity:
uv run artemis mcp --install antigravity
```

#### Manual JSON Config Generation
Run `uv run artemis mcp --generate-config antigravity` (or `all`) to output ready-to-use configuration JSON with resolved `.venv` Python and project paths.
