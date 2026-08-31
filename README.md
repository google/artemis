<p align="center">
  <img src="./docs/assets/artemis-banner.png?v=6" alt="ARTEMIS Banner" width="100%" />
</p>

<p align="center">
  <strong>Let AI assistants and test suites use real phones like a human.</strong>
</p>

<p align="center">
  <a href="./README.md"><b>English</b></a> •
  <a href="./README_CN.md">中文文档</a> •
  <a href="#workflow-showcase">Workflow Showcase</a> •
  <a href="#quick-start">Quick Start</a> •
  <a href="#mcp-setup">MCP for IDEs</a> •
  <a href="#benchmarks">Benchmarks</a> •
  <a href="https://discord.gg/wF2FN4WHGY">Discord Community</a>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.12+-3776AB.svg?logo=python&logoColor=white" alt="Python 3.12+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License: Apache-2.0"></a>
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/MCP-Native%20Server-8A2BE2.svg" alt="MCP Native"></a>
  <a href="https://ai.google.dev/"><img src="https://img.shields.io/badge/Multimodal-Gemini%20%7C%20Claude%20%7C%20GPT--4o%20%7C%20Qwen--VL-4285F4.svg" alt="Multi-Model"></a>
  <a href="https://github.com/google-research/android_world"><img src="https://img.shields.io/badge/AndroidWorld-99%25%2B%20SOTA-success.svg" alt="AndroidWorld SOTA"></a>
</p>

<!-- Demo Showcase -->
<p align="center">
  <img src="./docs/assets/demo.gif" alt="Artemis in Action" width="100%" />
  <br>
  <em>Live Demo: Setup driving routes and calculate total durations in Google Maps, then open YouTube to play a Coldplay song.</em>
</p>

## Key Highlights

* **Cross-App Automation & Autonomous AI Assistant**: Operates not just as a robust testing framework, but as an autonomous agent capable of handling complex cross-app workflows and daily tasks via natural language;
* **Zero-Maintenance Test Automation**: Built upon a "Dynamic-First, Coordinate-Fallback" multimodal locating engine, eliminating fragile XPath/ID selector maintenance and remaining resilient to UI redesigns, system updates, and resolution drift;
* **One-Click Bug Repro & Logcat Diagnostics in IDE**: Native **Model Context Protocol (MCP)** integration allows **Antigravity, Claude Code, and Windsurf** to drive physical test devices via natural language, automatically capturing crash stacks from **Logcat** and keyframe screenshots;
* **Ultra-Fast Execution (3–5s per Step)**: Pioneered an **Optimistic Asynchronous Pipeline** that completely decouples UI interaction from heavy LLM reasoning, achieving rapid regression throughput in Flash mode;
* **Popup Self-Healing & 10+ Hour Exploration**: Proprietary **Safety Net** double-checks targets before action execution to intercept and clear interfering system popups; Pro mode supports **10+ hours** of continuous exploratory & monkey-plus stability testing;
* **Industry-Leading SOTA**: Achieved **99%+ task completion** on Google Research's **AndroidWorld** benchmark (100+ complex multi-step tasks).

<a id="workflow-showcase"></a>
## Antigravity × ARTEMIS: Autonomous Testing Workflow

Experience seamless collaboration between **Antigravity** and **ARTEMIS** via native MCP integration — taking you from a natural language requirement to a production-grade diagnostic report in four automated steps:

<table width="100%">
  <tr>
    <td width="50%" align="center">
      <b>1. Prompt Input (Task Dispatch)</b><br>
      <sub>Describe your test scenario and target metrics in Antigravity</sub><br><br>
      <img src="./docs/assets/workflow-1-prompt.png" width="100%" alt="Step 1: Prompt Input in Antigravity" />
    </td>
    <td width="50%" align="center">
      <b>2. Test Plan Generation</b><br>
      <sub>Formulates a step-by-step test plan & architecture for review</sub><br><br>
      <img src="./docs/assets/workflow-2-plan.png" width="100%" alt="Step 2: Test Plan Generation" />
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <b>3. Autonomous Test Execution</b><br>
      <sub>Drives real device, navigates UI, and profiles performance</sub><br><br>
      <img src="./docs/assets/workflow-3-exec.png" width="100%" alt="Step 3: Autonomous Test Execution" />
    </td>
    <td width="50%" align="center">
      <b>4. Comprehensive Final Report</b><br>
      <sub>Delivers structured audit findings, metric tables, and raw datasets</sub><br><br>
      <img src="./docs/assets/workflow-4-report.png" width="100%" alt="Step 4: Final Report" />
    </td>
  </tr>
</table>

<a id="quick-start"></a>
## Quick Start

Ensure an Android device (with **USB Debugging** enabled) or emulator is connected. The one-click startup script will automatically:
- **Install System Toolchains**: Detect and auto-install ADB, scrcpy, FFmpeg, and Python (`uv`) dependencies.
- **Mount Global MCP Server & AI Agent Rules**: Prompt to automatically install global MCP configurations and the **Artemis Mobile Testing Mindset (`rules.md`)** into your AI IDEs (**Antigravity**, **Cursor**, **Claude Code**, **Codex**, **Windsurf**, **VS Code**, **Cline/Roo**, **OpenClaw**).

### macOS and Linux

```bash
# 1. Clone repo & navigate to directory
git clone https://github.com/google/artemis.git && cd artemis

# 2. One-click launch
./start.sh
```

### Windows PowerShell

```powershell
# 1. Clone repo & navigate to directory
git clone https://github.com/google/artemis.git
cd artemis

# 2. One-click launch
.\start.bat
```

> PowerShell does not search the current directory for executable scripts by default, so use `.\start.bat` without a trailing `\`. In Command Prompt (CMD), use `start.bat` instead.

> **Tip**: Opens `http://localhost:8000` in your default browser with a device connection wizard, live screen mirroring, prompt sandbox, and execution replays. You can also run directly from CLI: `uv run artemis run "Open Settings, find Battery and tell me current level" --profile flash`.

<a id="mcp-setup"></a>
<a id="mcp"></a>
<details>
<summary><b>MCP Setup for Codex / Antigravity / Claude Code / Windsurf (Click to expand)</b></summary>

<br>

ARTEMIS includes a native **Model Context Protocol (MCP)** server. Connect your real phone directly into AI IDEs:

### 1. One-Click Auto Install (Recommended)

Running `./start.sh` (macOS/Linux) or `.\start.bat` (Windows PowerShell) will prompt you to configure global MCP and testing rules for detected IDEs (or you can install/update anytime later manually using the commands below):

```bash
# Auto-install MCP server & global rules for Antigravity / Jetski:
uv run artemis mcp --install antigravity

# Or install for all supported AI IDEs (including Codex):
uv run artemis mcp --install all
```

> **Tip**: You can also configure MCP interactively during first-time setup via `uv run artemis init`.
> **Pro Tip**: If you want to use the `artemis` command globally without `uv run` in any directory, run `uv tool install -e .` once in the project root.

### 2. Manual Configuration (Optional)

If you prefer to configure manually, run `uv run artemis mcp --generate-config <client>` (for example, `codex` or `antigravity`) to output the appropriate TOML or JSON snippet. Replace `/path/to/artemis` with your actual repo path and point `command` to your `.venv` Python executable:

* **Codex** (`~/.codex/config.toml`):
```toml
[mcp_servers.artemis]
command = "/path/to/artemis/.venv/bin/python"
args = ["-m", "mcp_server"]
cwd = "/path/to/artemis"

[mcp_servers.artemis.env]
PYTHONUNBUFFERED = "1"
PYTHONPATH = "/path/to/artemis"
```

* **Antigravity** (`~/.gemini/jetski/mcp_config.json`):
```json
{
  "mcpServers": {
    "artemis": {
      "command": "/path/to/artemis/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "cwd": "/path/to/artemis",
      "env": {
        "PYTHONUNBUFFERED": "1"
      },
      "tools": {
        "mobile_run_task": { "eager": true },
        "mobile_manage_task": { "eager": true },
        "mobile_get_device_state": { "eager": true },
        "mobile_inspect_trace": { "eager": true }
      }
    }
  }
}
```

* **Claude Desktop** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "artemis": {
      "command": "/path/to/artemis/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "cwd": "/path/to/artemis"
    }
  }
}
```

### 3. Mount Behavioral Rules for AI Agents (Highly Recommended)

To ensure your AI coding assistant acts with the rigor of a senior mobile test engineer and never hallucinates UI interactions, we provide a dedicated testing mindset rules file at [`mcp_server/rules.md`](./mcp_server/rules.md) (covering **Active Exploration before coding**, **Flash vs. Pro routing strategy**, **Latency & Timing compensation**, and the **"Dynamic-First, Coordinate-Fallback" locator pattern**).

You can mount or copy [`mcp_server/rules.md`](./mcp_server/rules.md) into your AI IDE's rule configuration:
* **Antigravity**: Add the contents of `rules.md` to your Workspace Rules, Global Rules settings, or agent instructions.
* **Claude Code**: Run `artemis mcp --install claude` to install the rules to `~/.claude/rules/artemis.md` (install to exactly one location — Claude Code loads both `~/.claude/CLAUDE.md` and `~/.claude/rules/*.md`, so duplicating the rules wastes context).
* **Cursor**: Copy the contents into `.cursorrules` or create a rule file at `.cursor/rules/artemis.mdc`.
* **Codex**: Add the contents to `~/.codex/AGENTS.md` (or the active `AGENTS.override.md`).
* **Windsurf / OpenClaw**: Add the rules to your workspace rules or global system prompts.

> For more details on the testing mindset and MCP architecture, see the [MCP Server README](./mcp_server/README.md).

### 4. Prompt Your Phone in the IDE Chat
In Codex, Antigravity, or Claude Code, simply prompt:
> *"Build the latest changes into an APK, install it on the connected device, open the login screen with a test account, verify if there are any unexpected popups after login, and return screenshots of the final page."*

</details>

<a id="python-sdk"></a>
<details>
<summary><b>Python SDK Integration (Click to expand)</b></summary>

<br>

Install the zero-runtime-dependency client on the development machine. ADB,
agents, models, and image processing remain on the device host:

```powershell
uv add "artemis-client @ git+https://github.com/google/artemis.git#subdirectory=packages/artemis-client"
```

```python
import asyncio
from artemis_client import ArtemisClient


async def main():
    client = ArtemisClient(
        "http://artemis-host:8000",
        device_serial="emulator-5554",  # optional: target specific device serial
        default_profile="flash",        # "flash" (fast reactive) or "pro" (deep reasoning)
    )

    result = await client.run(
        "Open System Settings, go to 'Battery', verify battery percentage is displayed, and check for any crash dialogs.",
    )

    assert result.succeeded, f"Test failed: {result.error or result.status}"
    print(f"✅ Test Passed! Device: {result.device_serial} | Trace ID: {result.trace_id}")


if __name__ == "__main__":
    asyncio.run(main())
```

</details>

## Usage Modes

<p align="center">
  <img src="./docs/assets/artemis-ui-showcase-en.png" alt="Artemis Web Console" width="100%" />
  <br />
  <sub><b>Console Overview</b>: <b>① View Switcher</b> (Home / Workspace) · <b>② Model & Replay</b> (Flash/Pro status & video replay) · <b>③ Live Agent Stream</b> (Action perception, target coordinates & structured results) · <b>④ Prompt Dock</b> (Natural language dispatch) · <b>⑤ Task Queue & Dashboard</b> (Lifecycle & history)</sub>
</p>

* **Web Visual Test Console (`uv run artemis ui`)**: Real-time screen projection and interactive panel, supporting natural language test dispatch, live reasoning telemetry, action trajectories, and execution replay; manage server lifecycle anytime from any terminal using `uv run artemis restart`, `uv run artemis stop`, and `uv run artemis status`;
* **Native MCP Protocol (IDE Collaboration)**: Operates as a standard MCP server seamlessly integrating with **Antigravity, Claude Code, Windsurf**, etc., directly driving real devices inside the IDE to verify bugs and run test cases;
* **Developer CLI (`uv run artemis run`)**: Direct terminal execution for automated test cases, exploratory stability inspection, or AndroidWorld benchmarks with high-fidelity structured terminal output;
* **Python SDK**: Integrates as a standard Python library into existing automated testing frameworks (e.g., pytest) or CI/CD pipelines with strongly typed Pydantic structured outputs and assertion support.

<a id="benchmarks"></a>
## Benchmarks: AndroidWorld (SOTA 99%+)

Evaluated on [AndroidWorld](https://github.com/google-research/android_world) — Google Research's gold-standard benchmark spanning 20+ real apps and 100+ complex multi-step tasks: **Artemis demonstrated exceptional robustness across the entire benchmark suite, achieving a 99%+ completion rate.**

<p align="center">
  <img src="./docs/assets/androidworld_leaderboard.png?v=2" alt="AndroidWorld Benchmark Comparison" width="100%" />
</p>

## How ARTEMIS is Architected

* **Pre-Touch Pixel Gate & Speculative Chaining**: Eliminates "silent misclicks" from inference latency race conditions. Milliseconds before dispatch, a local UI guard intercepts unexpected dialogs (0 tokens, 0 cloud wait), a Micro-ROI gate verifies target stability, and speculative chained taps hit transient UI (e.g. auto-fading video controls) before they expire;
* **Three-Layer Progressive Grounding Engine**: Fuses local OCR with accessibility hierarchies (~150ms, 0 tokens) to drive 85%+ of standard actions via drift-free numeric indices, gracefully falling back to spatial vision models for custom Canvas/Compose/Flutter UI and sandboxed CV probing for subtle pixel states;
* **Elastic Dual Engine with In-Flight Context Compactor**: Seamlessly toggles between high-throughput reactive CI loops (Flash Mode, 3–5s/step) and multi-step cognitive state graphs (Pro Mode), using background visual deltas and DOM pruning to slash token consumption by >70% for 10+ hours of continuous, unattended soak testing.

<p align="center">
  <img src="./docs/assets/artemis_architecture_diagram.png" alt="ARTEMIS System Architecture Diagram" width="100%" />
</p>

## Execution Profiles: Flash vs. Pro

ARTEMIS supports two execution profiles tailored for different automation requirements:

* **Flash Profile (`--profile flash`)**: Fast and token-efficient reactive loop (~3–5s per step). Ideal for routine, deterministic UI tasks within 25–30 steps. *Limitations*: Does not support long-term state monitoring, video stream analysis, or multi-step failure self-healing.
* **Pro Profile (`--profile pro`)**: Our most capable model architecture (~15–40s per step). Powered by a multi-agent graph with planning, visual verification, and automated recovery. Capable of handling 100+ step long-horizon workflows, continuous device state monitoring, and multimedia video analysis.

## Roadmap

- [ ] **Android Studio Integration**: Native IDE plugin and workflow integration to enable in-editor debugging, test recording, and automated device control directly within Android Studio.
- [ ] **iOS Platform Expansion**: Extending multimodal perception and mobile automation to iOS devices and simulators.
- [ ] **On-Device Lightweight VLMs**: Local execution with lightweight edge vision models for low-latency, privacy-first automation.
- [ ] **Real-time Duplex Voice Interaction**: Voice-driven task dispatch with real-time conversational control and interruption handling.

## Community & Contributing

Contributions are warmly welcomed!
* **Star the repo** to follow updates and releases
* Join the [Discord Community](https://discord.gg/wF2FN4WHGY) for technical discussions
* Open an [Issue](https://github.com/google/artemis/issues) or submit a [Pull Request](https://github.com/google/artemis/pulls)

## License

This project is licensed under the [Apache License 2.0](LICENSE).
