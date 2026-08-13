# ARTEMIS ☕📱

**Autonomous Multimodal Android Agent & Mobile UI Automation Platform**

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Framework: LangGraph](https://img.shields.io/badge/Orchestration-LangGraph-orange.svg)](https://github.com/langchain-ai/langgraph)
[![Model: Google Gemini](https://img.shields.io/badge/Powered%20by-Google%20Gemini-4285F4.svg)](https://ai.google.dev/)

ARTEMIS is a next-generation, production-grade autonomous agent and testing framework engineered for Android devices and emulators. Combining state-of-the-art multimodal Large Vision-Language Models (VLMs) with hierarchical closed-loop planning, resilient self-healing, and deep Android subsystem integration (ADB, UIAutomator2, accessibility trees), ARTEMIS enables autonomous UI navigation, robust automated testing, and intelligent device diagnostics.

---

## 📑 Table of Contents

- [Key Features](#-key-features)
- [Architecture Overview](#-architecture-overview)
- [Prerequisites](#-prerequisites)
- [Installation & Setup](#-installation--setup)
- [Quick Start](#-quick-start)
  - [Command Line Interface (CLI)](#command-line-interface-cli)
  - [Python SDK](#python-sdk)
- [Execution Profiles: Flash vs. Pro](#-execution-profiles-flash-vs-pro)
- [Model Context Protocol (MCP) Integration](#-model-context-protocol-mcp-integration)
- [Configuration Guide](#-configuration-guide)
  - [Using Non-Google LLMs](#using-non-google-llms)
- [Trace Inspection & Observability](#-trace-inspection--observability)
- [Development & Testing](#-development--testing)
- [License](#-license)

---

## ✨ Key Features

- **Dual-Engine Execution**:
  - **ARTEMIS Flash**: High-speed, reactive single-agent loop for deterministic, low-latency tasks (< 35 steps).
  - **ARTEMIS Pro**: Multi-agent closed-loop graph orchestration (Planner $\rightarrow$ Operator $\rightarrow$ Failure Analyzer $\rightarrow$ Summarizer) powered by LangGraph.
- **Multimodal Perception**:
  - Combined visual screenshot grounding, real-time OCR, accessibility hierarchy (XML) parsing, and dynamic video analysis.
- **Resilient Locating Strategy ("Dynamic-First, Coordinate-Fallback")**:
  - Prioritizes semantic IDs, XPath, and accessibility text matching to withstand UI layout drift; seamlessly falls back to calibrated absolute/relative coordinates.
- **Autonomous Self-Healing**:
  - Built-in failure analyzer and safety net detect blocked flows, system dialogs, app crashes, or unexpected states and auto-recover in real time.
- **App Locking & Session Boundaries**:
  - Restricts execution context to target application packages with automatic foreground recovery.
- **Model Context Protocol (MCP) Native**:
  - Exposes Android automation, state inspection, and trace querying as MCP tools for IDEs and AI orchestrators.
- **Structured Pydantic Outputs**:
  - Automatically parses agent observations and findings into validated Pydantic schemas.

---

## 🏛 Architecture Overview

ARTEMIS operates on a closed-loop multi-agent architecture:

```
                      ┌─────────────────────────────────┐
                      │          User Goal / Task       │
                      └────────────────┬────────────────┘
                                       │
                                       ▼
                       ┌───────────────────────────────┐
                       │       Planner Agent           │
                       │ (Hierarchical Task Breakdown) │
                       └───────────────┬───────────────┘
                                       │
                        ┌──────────────┴──────────────┐
                        ▼                             ▼
       ┌─────────────────────────────────┐   ┌───────────────────────────────┐
       │         Operator Agent          │   │      Diagnostic Agents        │
       │ (Screen VLM + XML Grounding)    │   │  (Log Reader, Video Analyzer, │
       └────────────────┬────────────────┘   │   Object Detector, Explorer)  │
                        │                    └───────────────────────────────┘
                        ▼
       ┌─────────────────────────────────┐
       │   Execution & Device Control    │
       │   (ADB / UIAutomator2 / Taps)   │
       └────────────────┬────────────────┘
                        │
          [Action Success / State Change?]
            │                         │
          (Yes)                      (No / Blocked)
            │                         │
            ▼                         ▼
 ┌──────────────────────┐  ┌─────────────────────────────────────────┐
 │ Next Step / Complete │  │ Failure Analyzer & Safety Net           │
 └──────────┬───────────┘  │ (Popup dismissal, Retry, Self-healing)  │
            │              └────────────────────┬────────────────────┘
            ▼                                   │
 ┌──────────────────────┐                       │
 │  Summarizer / Output │◄──────────────────────┘
 │  (Reports, Traces)   │
 └──────────────────────┘
```

---

## 🔧 Prerequisites

1. **Python**: Version `3.12` or higher.
2. **Android SDK / ADB**:
   - `adb` must be installed and accessible in your system `PATH`.
   - Verify with `adb devices`.
3. **Android Device or Emulator**:
   - A physically connected Android device with **USB Debugging** enabled, or
   - An active Android Emulator (AVD) running on your local machine.
4. **Screen Recording & Video Tools** *(optional for `--with-video-recording-tools`)*:
   - **`scrcpy`**: Required for live Android device screen video recording (`apt install scrcpy` / `brew install scrcpy`).
   - **`ffmpeg`**: Required for video transcoding, trimming, and audio extraction (`apt install ffmpeg` / `brew install ffmpeg`).

---

## 🚀 Installation & Setup

### 1. Clone the Repository

```bash
git clone https://github.com/google/artemis.git
cd artemis
```

### 2. Install Dependencies

Using [`uv`](https://docs.astral.sh/uv/) (recommended):

```bash
# Install dependencies into a virtual environment
uv sync --dev
```

Or using standard `pip`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Quick Setup (10 Seconds)

Run the interactive setup wizard to configure your API key and connect your device:

```bash
artemis init
```

Verify your environment and connected devices anytime with the doctor diagnostic tool:

```bash
artemis doctor
```

*(Alternatively, copy `.env.example` to `.env` and set `GEMINI_API_KEY` for LLM reasoning and `OCR_API_KEY` for Vision perception.)*

---

## 💡 Quick Start

### Command Line Interface (CLI)

ARTEMIS installs a unified command-line tool `artemis`:

```bash
# 1. Environment & Device Diagnostics
artemis doctor

# 2. Interactive Setup Wizard
artemis init

# 3. Basic task execution (ARTEMIS Pro by default)
artemis run "Open YouTube and search for Lo-Fi Hip Hop"

# 4. Fast reactive profile (ARTEMIS Flash)
artemis run "Open Settings and verify Airplane Mode is turned off" --profile flash

# 5. Execute with trace recording and structured output
artemis run "Open Maps, search for coffee shops, and extract the top 3 results" \
  --test-name "coffee_search_test" \
  --traces-path "./traces" \
  --output-description '{"places": "list of shop names", "ratings": "list of rating scores"}'

# 6. Enable dynamic video recording analysis
artemis run "Play the latest video in the feed and verify playback" --with-video-recording-tools
```

### Python SDK

Use the fluent SDK to integrate ARTEMIS into your test suites or automation workflows:

```python
import asyncio
from pydantic import BaseModel, Field
from artemis.sdk import Agent


class VideoSearchResult(BaseModel):
    query: str = Field(..., description="The search term used")
    top_video_title: str = Field(..., description="Title of the first video result")
    channel_name: str = Field(..., description="Channel name of the first video")


async def main():
    # 1. Initialize the agent
    agent = Agent()
    await agent.init()

    try:
        # 2. Build task specification
        task = (
            agent.new_task("Search for 'Pixel 9 Pro' and get the first video details")
            .with_name("youtube_search_verification")
            .with_locked_app_package("com.google.android.youtube")
            .with_output_format(VideoSearchResult)
            .with_max_steps(50)
            .build()
        )

        # 3. Execute task
        result: VideoSearchResult = await agent.run_task(request=task)

        if result:
            print("Search Results:")
            print(f"- Query: {result.query}")
            print(f"- First Video: {result.top_video_title}")
            print(f"- Channel: {result.channel_name}")
        else:
            print("Task did not complete successfully.")

    finally:
        # 4. Clean up agent session & device connections
        await agent.clean()


if __name__ == "__main__":
    asyncio.run(main())
```

---

## ⚡ Execution Profiles: Flash vs. Pro

| Feature | ARTEMIS Flash (`--profile flash`) | ARTEMIS Pro (`--profile default`) |
| :--- | :--- | :--- |
| **Model Loop** | Single reactive agent (Observe-Think-Act) | Multi-node LangGraph orchestration |
| **Best For** | Short, deterministic tasks (< 35 steps) | Deep, complex, multi-stage workflows |
| **Step Latency** | ~3–5 seconds / step | ~15–30 seconds / turn (with planning & validation) |
| **Planning** | Direct goal execution | Hierarchical multi-step plan decomposition |
| **Diagnostics** | Basic error reporting | Deep diagnostics, log analysis & self-healing |

---

## 🔌 Model Context Protocol (MCP) Integration

ARTEMIS includes built-in MCP servers and tools to allow AI IDEs (such as Claude Desktop, Cursor, and Gemini Jetski) to control Android devices directly:

| Tool Name | Description |
| :--- | :--- |
| `mobile_run_task` | Launches an autonomous background automation task on the connected device. |
| `mobile_manage_task` | Inspects status, terminates tasks, or injects real-time steering instructions. |
| `mobile_get_device_state` | Retrieves real-time screenshots or simplified accessibility UI trees. |
| `mobile_inspect_trace` | Inspects step-by-step screenshots, visual action overlays, and execution logs. |

---

## ⚙️ Configuration Guide

All model configurations, thinking budgets, agent capabilities, and tool filtering are consolidated into a single self-explanatory configuration file: [`config/artemis.jsonc`](config/artemis.jsonc).

```jsonc
{
  // 1. Global Default Model (Applied across all sub-agents unless overridden)
  "default": {
    "provider": "google",
    "model": "gemini-3.6-flash",
    "thinking_level": "medium",
    "fallback": {
      "provider": "google",
      "model": "gemini-3.5-flash"
    }
  },

  // 2. Built-in Presets
  "presets": {
    "gemini-flagship": { "provider": "google", "model": "gemini-3.6-flash" },
    "openai-gpt4o": { "provider": "openai", "model": "gpt-4o" },
    "cost-saving": { "provider": "google", "model": "gemini-3.5-flash-lite" },
    "local-ollama": { "provider": "openai", "model": "llama3.2-vision" }
  },

  // 3. Agent & Sub-Agent Node Customizations (Optional overrides)
  "nodes": {
    // 🧠 Planner: High-level goal decomposition and strategic step planning
    "planner": { "thinking_level": "medium" },

    // 🎯 Operator: Screen perception, element interaction, and direct execution
    "operator": { "thinking_level": "medium", "include_thoughts": true },

    // 🛡️ Validator & Failure Analyzer: Self-healing and auto-recovery
    "validator_failure_analyzer": { "thinking_level": "medium" }
  },

  // 4. Agent Capabilities & Tool Filtering
  "agent": {
    "explorer_versions": { "operator": "flash", "validator": "flash" },
    "blacklisted_tools": {
      "explorer": ["ask_image_processor", "get_ocr_list", "inspect_region"]
    }
  }
}
```

> [!TIP]
> **Zero-Friction Global Change**: To switch the entire platform from Gemini to GPT-4o or Claude, simply edit the `"default"` block once in `config/artemis.jsonc`. All 15 sub-agents automatically inherit the new model!

### Using Non-Google LLMs

ARTEMIS supports multi-provider LLM backends via standard LangChain integrations (Google Gemini, OpenAI, OpenRouter, xAI / Grok, or Google Vertex AI).

#### Supported Providers & API Key Setup

| Provider | Provider Key | Environment Variable | Base URL Override | Example Models |
| :--- | :--- | :--- | :--- | :--- |
| **Google Gemini** | `google` *(default)* | `GEMINI_API_KEY` | N/A | `gemini-3.6-flash`, `gemini-3.5-flash` |
| **Google Vision OCR** | `ocr` | `OCR_API_KEY` | N/A | Google Cloud Vision Text Detection |
| **Google Vertex AI** | `vertexai` | Google ADC (`gcloud auth application-default login`) | N/A | `gemini-3.6-flash` |
| **OpenAI** | `openai` | `OPENAI_API_KEY` | `OPENAI_BASE_URL` | `gpt-4o`, `gpt-4o-mini`, `o3` |
| **OpenRouter** | `openrouter` | `OPEN_ROUTER_API_KEY` | N/A | `anthropic/claude-3.5-sonnet`, `meta-llama/llama-3.3-70b-instruct` |
| **xAI (Grok)** | `xai` | `XAI_API_KEY` | N/A | `grok-2-vision-1212`, `grok-beta` |

> [!NOTE]
> ARTEMIS uses multimodal visual grounding (screenshots) for UI navigation. When assigning non-Google models to vision-dependent agents (such as `operator`), ensure the selected model supports image/vision inputs.

#### Switching Provider in `config/artemis.jsonc`

To use OpenAI or Claude globally, simply change the `default` section in [`config/artemis.jsonc`](config/artemis.jsonc):

```jsonc
{
  "default": {
    "provider": "openai",
    "model": "gpt-4o",
    "fallback": {
      "provider": "openai",
      "model": "gpt-4o-mini"
    }
  }
}
```

#### Custom / Local OpenAI-Compatible Endpoints (Ollama, vLLM, LocalAI)

To connect ARTEMIS to local or self-hosted LLM endpoints (e.g., Ollama or vLLM), set `OPENAI_BASE_URL` in `.env` and specify `openai` as the provider in your configuration:

```bash
# Route OpenAI provider calls to local Ollama / vLLM endpoint
OPENAI_API_KEY=ollama  # dummy key if required by client
OPENAI_BASE_URL=http://localhost:11434/v1
```

---

## 📊 Trace Inspection & Observability

Every task execution can record rich execution artifacts stored in `./traces/<trace_id>/`:
- **`notes/`**: High-level planner notes, step summaries, and generated reports (`output.md`).
- **`screenshots/`**:
  - `before_screenshot.jpg`: Initial screen observed by the agent.
  - `action_overlay_screenshot.jpg`: Screen with visual tap markers (red circles) and swipe vectors (red arrows).
- **`logs/`**: Raw framework, operator, and failure analyzer diagnostic logs.

---

## 🛠 Development & Testing

ARTEMIS provides standard `make` targets for development:

```bash
# Setup environment and pre-commit hooks
make setup

# Run test suite
make test

# Run code formatters (ruff)
make format

# Run linter checks
make lint

# Run static type checker (pyright)
make typecheck
```

---

## 📄 License

This project is licensed under the [Apache License 2.0](LICENSE).
