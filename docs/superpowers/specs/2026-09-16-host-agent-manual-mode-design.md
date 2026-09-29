# Host-Agent-Driven Manual Mode (`mobile_observe` / `mobile_act`)

**Status:** Draft
**Date:** 2026-09-16

## Problem

Every existing Artemis execution path (`mobile_run_task` with `Flash` or `Pro`)
requires a multimodal LLM API key configured server-side
(`GEMINI_API_KEY`/`OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/...) in `artemis/.env`,
because `FlashRunner` and the Pro graph (`Planner`/`Operator`/`Checker`/
`Explorer`) each call `artemis/llm/router.py::ModelFactory` directly to make
their own "observe screen -> decide -> act" reasoning calls.

Many Artemis users already run inside an MCP host that *is itself* an
AI agent with its own model access and no separate key requirement to the
caller (Claude Code, Codex, Antigravity, and similar coding agents). Today
there is no way for that host's own intelligence to drive an Artemis-managed
device directly — the host would have to shell out to raw `adb` itself,
duplicating device-driving logic (screenshot capture, OCR/XML fusion,
element indexing, coordinate translation, swipe smoothing, etc.) that already
exists inside Artemis.

## Goals

- Let any MCP host with its own model reasoning drive a connected Android
  device through Artemis's existing, battle-tested device-driving code,
  without configuring any LLM credential in Artemis itself.
- Reuse Flash's exact action surface and observation format, so a host
  agent effectively runs the same reactive loop Flash runs autonomously,
  just with the host supplying each decision.
- Do not disturb the existing `mobile_run_task` (Flash/Pro) behavior or
  its background/daemon execution model.

## Non-goals

- Wiring real MCP `sampling/createMessage` into `FlashRunner` or the Pro
  graph. Investigated and rejected for this iteration — see "Rejected
  approach" below.
- Giving the new mode access to tools that themselves invoke Artemis's LLM
  router (`ask_explorer`, `video_analyzer`, note/history tools). Those need
  their own credentialed sub-agent call regardless of who drives the top
  loop, so exposing them here would silently reintroduce the API-key
  requirement this feature exists to remove.
- Verification/checkpoint infrastructure (Pro's Checker). The host agent is
  its own verifier by construction — it decides when the goal is met.

## Rejected approach: MCP sampling into Flash/Pro

MCP's `sampling/createMessage` lets a server ask the connected client to
fulfill a completion using the client's own model. It looks like a direct
fit, but two properties of the existing architecture make it the wrong tool
here:

1. **No native tool-calling in the sampling spec.** Flash/Pro bind
   structured tools (`click_sequence`, `report_task_status`, ...) via
   LangChain's `bind_tools()`; sampling only returns free text, so every
   action would need a prompt-encoded-JSON shim to parse back into a tool
   call — a compatibility layer, not a clean interface.
2. **Session lifetime mismatch.** `mobile_run_task` deliberately spawns a
   detached background subprocess/daemon (`mcp_server/tools/task_runner.py`)
   so the tool call returns immediately and the caller polls for status.
   MCP sampling requires calling back over the *same live client
   connection* that issued the original tool call. Bridging that would mean
   the background runner blocks on every single decision waiting for a
   relayed round trip to the original caller — turning today's
   fire-and-forget, pollable task model into one where the calling agent
   must stay synchronously engaged for the entire task duration. That is a
   much larger behavioral change than it appears and risks breaking the
   existing UX for every current `mobile_run_task` caller.

Instead, this design adds a **new, third mode** alongside Flash and Pro,
built from already-existing internals, that sidesteps both problems by
never spawning a background task in the first place — the host agent's own
foreground request/response loop *is* the execution loop.

## Design

### Reused components (unchanged)

- `artemis/mcp/observation.py::observe()` — screen capture, OCR/XML fusion,
  indexed "Visible UI Elements" list. Identical output shape to what Flash's
  own prompt shows the model each turn.
- `artemis/mcp/action_executor.py::McpActionExecutor` — argument
  translation, action dispatch through `ActionSession`, and the
  automatic post-action `observe()` call. This is Flash's actual "hands";
  reusing it means the new mode inherits Flash's action semantics exactly
  (including its known limitation of no pre-execution safety net — that is
  a Pro/Validator-only feature and stays out of scope here).
- `artemis/runtime/device_lock.py::DeviceExecutionLock` — the existing
  cross-process per-device mutex. The new mode acquires it like any other
  task, so it cannot run concurrently with a Flash/Pro task on the same
  device, and vice versa.
- `artemis/mcp/action_manifest.py` — `REQUIRED_ACTIONS`/`OPTIONAL_ACTIONS`
  define the exact action vocabulary exposed.

### New: session registry

A new module, `mcp_server/manual_session.py`, holds an in-process registry
(dict keyed by normalized device serial) of active manual sessions:

```python
@dataclass
class ManualSession:
    ctx: ArtemisContext
    controller: UnifiedMobileController
    executor: McpActionExecutor
    indexed_elements: list[dict]
    indexed_points: list[list[int]]
    latest_screenshot: str | None
    lock_token: str
    last_used: float
```

- Lazily created on the first `mobile_observe`/`mobile_act` call for a
  device serial. Creation acquires `DeviceExecutionLock` for that device
  the same way `mobile_run_task` does; failure to acquire (device busy with
  a Flash/Pro task, or another manual session) surfaces as a clear
  "device busy" tool error rather than blocking silently.
- `indexed_elements`/`indexed_points`/`latest_screenshot` are the minimal
  fields `McpActionExecutor` actually reads/writes (see
  `action_executor.py::_execute_device_action`, `_resolve_index`). The
  session does **not** reuse the full LangGraph `State` object Flash/Pro
  use internally — that object carries agent-loop fields (transcript,
  turn counters, checkpoints, ...) that have no meaning for a
  session with no LLM loop of its own. A minimal dataclass matching only
  the fields the executor touches is the correct-sized reuse.
- Reaped by a background idle-timeout sweep (default 10 minutes,
  configurable via `ARTEMIS_MANUAL_SESSION_TIMEOUT_S`) that releases the
  device lock and drops the session, so a caller that forgets to close a
  session doesn't permanently lock a device out of other Artemis use.
- Explicitly released by `mobile_session_end`.

### New MCP tools (`mcp_server/tools/manual_mode.py`)

- **`mobile_observe(device_serial: str | None = None) -> dict`**
  Returns `{screenshot: <file URI>, elements_text: <numbered list>,
  width, height, device_serial}`. Creates/reuses the session for that
  device.

- **`mobile_act(action: str, args: dict, device_serial: str | None = None) -> dict`**
  Validates `action` is one of the allowed device actions (see below),
  runs it through the session's `McpActionExecutor.execute()`, updates the
  session's `indexed_elements`/`indexed_points`/`latest_screenshot` from the
  result, and returns
  `{status: "success"|"error", text_summary, screenshot, elements_text}` —
  the same shape as `mobile_observe`, so the caller's loop is a plain
  observe/act/observe cycle without a separate "refresh" step.

  Allowed actions (mirrors Flash's device-action set exactly):
  `click`, `long_press`, `input_text`, `click_sequence`, `swipe`,
  `press_key`, `manage_app`, `wait_for_delay`, `wait_for_text`,
  `open_link`, `erase_one_char`, `focus_and_clear_text`.
  Anything outside this set (including `ask_explorer`, `video_analyzer`,
  and note/history tools) is rejected with an explicit error naming why
  (those tools invoke Artemis's own LLM router internally).

- **`mobile_session_end(device_serial: str | None = None) -> dict`**
  Releases the device lock and drops the session immediately. Idempotent —
  ending an already-ended/nonexistent session is not an error.

### Error handling

`McpActionExecutor`'s existing `ToolExecutionResult` (status + text_summary)
is surfaced directly as the tool's return value — a host agent sees the
same actionable error text ("Invalid target index 4. Active index range is
1 to 7...") that Flash itself would see and would already know how to react
to, since it is the identical message format.

### Testing

Per `CONTRIBUTING.md`'s required `make test` suite (no device/credentials
needed):

- Unit tests using the existing `artemis/mcp/actuators/mock.py` mock
  actuator, covering: session creation and reuse across calls, idle-timeout
  reaping, `DeviceExecutionLock` contention (manual session vs. running
  Flash/Pro task on the same device, and vs. a second manual session),
  action-set rejection for out-of-scope tools, and argument-translation
  error passthrough.

`make test-device` (requires an attached/authorized device, marked
`android`):

- One end-to-end test exercising a real `mobile_observe` -> `mobile_act`
  (`click` by element index) -> `mobile_observe` cycle, asserting the
  returned element list changes and no device lock is left held after
  `mobile_session_end`.

### Documentation

Update `README.md`/`README_CN.md`'s tool listing to describe the new mode
alongside `mobile_run_task`, explicitly noting it needs no LLM credential
and is meant for MCP hosts that already have their own model.

## Open questions for implementation

- Exact `ARTEMIS_MANUAL_SESSION_TIMEOUT_S` default (proposed: 600s / 10 min)
  — confirm against how long a realistic host-driven test run should be
  allowed to sit idle (e.g., host agent pausing for user input mid-task)
  without losing its device lock.
- Whether `mobile_act`'s `manage_app` should be allowed to *install* an APK
  (parity with `mobile_run_task(app_path=...)`) or only launch/stop already
  installed packages. Proposed: launch/stop only for the first cut; a
  separate `app_path` install affordance can be added later if needed.
