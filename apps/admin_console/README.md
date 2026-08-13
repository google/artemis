# 🛠️ Artemis Admin & Trace Console

A comprehensive developer/administrator control plane and trace inspection console for the Latte/Artemis mobile automation system.

---

## 🎯 Key Features

1. **Full Trace Inspection (`/api/sessions`, `/api/steps`)**:
   - Hierarchical Call Tree exploration (LLM thoughts, tool calls, agent motivations).
   - Step-by-step pre/post screenshots and action overlays (tap circles, swipe paths).
   - Streaming logs and video recording playback.
2. **Task Control Center (`/api/run`)**:
   - Directly launch autonomous tasks with custom goals and model profiles (Flash / Pro).
   - Pause, resume, and manage FIFO task execution queues.
3. **Step Replay & Debugging Engine (`/api/replay`)**:
   - Replay individual steps with isolated inputs, state inspection, and live device execution.

---

## 🚀 How to Start

### Option 1: Using `uv` (Recommended)
From the repository root:
```bash
uv run uvicorn apps.admin_console.server:app --host 0.0.0.0 --port 8000 --reload
```
Or directly using Python module:
```bash
uv run python -m apps.admin_console.server
```

### Option 2: From the `apps/admin_console` directory
```bash
cd apps/admin_console
uv run uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

Once started, open your browser and navigate to:
👉 **`http://localhost:8000`**

---

## 🎥 Video Debugging Mode

By default, `VideoAnalyzer` cleans up local temporary video files after execution. To retain video recordings for full playback in the console:

```bash
KEEP_VIDEOS=1 artemis run "your task goal"
```
Or set `ARTEMIS_DEBUG=1` in your environment.
