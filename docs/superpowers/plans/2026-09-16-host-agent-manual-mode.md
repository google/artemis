# Host-Agent-Driven Manual Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `mobile_observe`, `mobile_act`, and `mobile_session_end` MCP tools so any MCP host with its own model (Claude Code, Codex, Antigravity, etc.) can drive a connected Android device through Artemis's existing observation/action-executor internals, without configuring an LLM credential in Artemis.

**Architecture:** A new `ManualSessionRegistry` (in-process, keyed by device serial) lazily builds a `McpActionExecutor` per device on first use, guarded by the existing `DeviceExecutionLock` so it can't run concurrently with a Flash/Pro task on the same device. Three thin MCP tools call into the registry and the executor; no new LLM provider, no background subprocess.

**Tech Stack:** Python 3.11+, FastMCP (`mcp.server.fastmcp`), pytest, existing Artemis internals (`artemis.mcp.action_executor`, `artemis.mcp.adb_server`, `artemis.runtime.device_lock`, `artemis.mcp.actuators.mock` for tests).

**Spec:** `docs/superpowers/specs/2026-09-16-host-agent-manual-mode-design.md`

---

## File Structure

- Create: `mcp_server/manual_session.py` — `ManualSession` dataclass + `ManualSessionRegistry`.
- Create: `mcp_server/tools/manual_mode.py` — `mobile_observe`, `mobile_act`, `mobile_session_end`.
- Modify: `mcp_server/tools/__init__.py` — register the three new tools.
- Modify: `mcp_server/base.py` — mention the new tools in the shared FastMCP instructions string.
- Modify: `README.md` — document the new mode next to `mobile_run_task`.
- Create: `tests/unit/mcp_server/test_manual_session.py`
- Create: `tests/unit/mcp_server/test_manual_mode_tools.py`
- Create: `tests/e2e/test_manual_mode_device.py` (marked `android`, requires a real device — not run by `make test`)

---

### Task 1: `ManualSession` / `ManualSessionRegistry`

**Files:**
- Create: `mcp_server/manual_session.py`
- Test: `tests/unit/mcp_server/test_manual_session.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/mcp_server/test_manual_session.py
"""Session lifecycle: creation, reuse, device-lock contention, idle reaping."""

from unittest.mock import Mock

import pytest

from mcp_server.manual_session import ManualSession, ManualSessionRegistry
from artemis.runtime.device_lock import DeviceBusyError, DeviceExecutionLock


def _fake_executor_factory():
    calls: list[str | None] = []

    def factory(device_serial: str | None):
        calls.append(device_serial)
        return Mock(name=f"executor-{len(calls)}")

    factory.calls = calls
    return factory


def test_get_or_create_reuses_session_for_same_device():
    factory = _fake_executor_factory()
    registry = ManualSessionRegistry(executor_factory=factory)
    try:
        first = registry.get_or_create("dev-reuse")
        second = registry.get_or_create("dev-reuse")
        assert first is second
        assert factory.calls == ["dev-reuse"]
    finally:
        registry.end("dev-reuse")


def test_get_or_create_is_isolated_per_device():
    factory = _fake_executor_factory()
    registry = ManualSessionRegistry(executor_factory=factory)
    try:
        a = registry.get_or_create("dev-a")
        b = registry.get_or_create("dev-b")
        assert a is not b
        assert factory.calls == ["dev-a", "dev-b"]
    finally:
        registry.end("dev-a")
        registry.end("dev-b")


def test_get_or_create_none_serial_uses_default_key():
    factory = _fake_executor_factory()
    registry = ManualSessionRegistry(executor_factory=factory)
    try:
        first = registry.get_or_create(None)
        second = registry.get_or_create(None)
        assert first is second
    finally:
        registry.end(None)


def test_get_or_create_raises_when_device_locked_by_another_owner():
    other = DeviceExecutionLock(
        device_id="dev-busy", description="other Artemis task", session_id="other-task"
    )
    other.acquire(blocking=False)
    try:
        registry = ManualSessionRegistry(executor_factory=_fake_executor_factory())
        with pytest.raises(DeviceBusyError):
            registry.get_or_create("dev-busy")
    finally:
        other.release()


def test_end_releases_lock_and_allows_recreation():
    factory = _fake_executor_factory()
    registry = ManualSessionRegistry(executor_factory=factory)
    registry.get_or_create("dev-end")
    assert registry.end("dev-end") is True

    # The device lock must actually be released: another owner can now take it.
    other = DeviceExecutionLock(
        device_id="dev-end", description="other Artemis task", session_id="other-task"
    )
    other.acquire(blocking=False)
    other.release()

    registry.get_or_create("dev-end")
    registry.end("dev-end")
    assert factory.calls == ["dev-end", "dev-end"]


def test_end_on_unknown_device_is_not_an_error():
    registry = ManualSessionRegistry(executor_factory=_fake_executor_factory())
    assert registry.end("never-created") is False


def test_idle_session_is_reaped_on_next_registry_access():
    factory = _fake_executor_factory()
    registry = ManualSessionRegistry(executor_factory=factory, idle_timeout_s=0.0)
    registry.get_or_create("dev-idle")

    # idle_timeout_s=0.0 means the session is stale the instant any time passes;
    # the next access anywhere in the registry reaps it and creates a fresh one.
    registry.get_or_create("dev-idle")

    assert factory.calls == ["dev-idle", "dev-idle"]
    registry.end("dev-idle")


def test_manual_session_fields_default_empty():
    session = ManualSession(executor=Mock(), lock=Mock(), last_used=0.0)
    assert session.indexed_elements == []
    assert session.indexed_points == []
    assert session.latest_screenshot is None
    assert session.latest_ui_hierarchy is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/mcp_server/test_manual_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcp_server.manual_session'`

- [ ] **Step 3: Write the implementation**

```python
# mcp_server/manual_session.py
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""In-process registry of host-agent-driven manual device sessions.

A manual session pairs one connected Android device with a lazily created
``McpActionExecutor`` -- the same "hands" ``FlashRunner`` uses internally --
driven turn-by-turn by whichever MCP host is calling ``mobile_observe`` /
``mobile_act`` instead of an Artemis-internal LLM loop.

Idle sessions are reaped opportunistically on the next registry access rather
than by a background thread: this keeps the registry single-threaded and
simple, at the cost of a session only being released the next time *any*
device's session is touched (or explicitly via ``mobile_session_end``, which
is the expected common path for a host agent that knows it is done).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import time
from typing import Any

from artemis.mcp.action_executor import McpActionExecutor
from artemis.mcp.adb_server import _get_controller
from artemis.runtime.device_lock import DeviceBusyError, DeviceExecutionLock

__all__ = ["ManualSession", "ManualSessionRegistry", "DEFAULT_IDLE_TIMEOUT_S"]

#: Default seconds a session may sit unused before the next registry access
#: reaps it and releases its device lock. Overridable per-registry for tests.
DEFAULT_IDLE_TIMEOUT_S = 600.0


@dataclass
class ManualSession:
    """One device's live manual-mode state.

    Doubles as the ``state`` argument ``McpActionExecutor.execute()`` expects:
    it reads/writes exactly ``indexed_elements``, ``indexed_points``, and
    ``latest_screenshot`` (see ``artemis/mcp/action_executor.py``), and reads
    ``latest_ui_hierarchy`` (via ``getattr(..., None)``) for direction-swipe
    smart targeting.
    """

    executor: McpActionExecutor
    lock: DeviceExecutionLock
    last_used: float
    indexed_elements: list[dict] = field(default_factory=list)
    indexed_points: list[list[int]] = field(default_factory=list)
    latest_screenshot: str | None = None
    latest_ui_hierarchy: str | None = None


def _default_executor_factory(device_serial: str | None) -> McpActionExecutor:
    """Builds a real-device executor the same way ``mobile_get_device_state`` does."""
    controller = _get_controller(device_serial=device_serial)
    return McpActionExecutor(controller.ctx, controller, agent_name="manual")


class ManualSessionRegistry:
    """Creates, reuses, and reaps manual sessions, one per device key."""

    def __init__(
        self,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
        executor_factory: Callable[[str | None], McpActionExecutor] = _default_executor_factory,
    ):
        self._sessions: dict[str, ManualSession] = {}
        self._idle_timeout_s = idle_timeout_s
        self._executor_factory = executor_factory

    @staticmethod
    def _key(device_serial: str | None) -> str:
        return device_serial or "default"

    def _reap_idle(self) -> None:
        now = time.monotonic()
        stale_keys = [
            key
            for key, session in self._sessions.items()
            if now - session.last_used >= self._idle_timeout_s
        ]
        for key in stale_keys:
            self._end_key(key)

    def _end_key(self, key: str) -> bool:
        session = self._sessions.pop(key, None)
        if session is None:
            return False
        session.lock.release()
        return True

    def get_or_create(self, device_serial: str | None) -> ManualSession:
        """Returns the device's live session, creating and locking it if needed.

        Raises:
            DeviceBusyError: the device is already owned by another Artemis
                task (Flash/Pro run or a different manual session).
        """
        self._reap_idle()
        key = self._key(device_serial)
        session = self._sessions.get(key)
        if session is not None:
            session.last_used = time.monotonic()
            return session

        lock = DeviceExecutionLock(
            device_id=key,
            description="Artemis manual session (mobile_observe/mobile_act)",
            session_id=f"manual-{key}",
            ingress="mcp",
        )
        try:
            lock.acquire(blocking=False)
        except DeviceBusyError as exc:
            raise DeviceBusyError(
                f"Device '{key}' is busy with another Artemis task or manual "
                f"session: {exc}"
            ) from exc

        executor = self._executor_factory(device_serial)
        session = ManualSession(executor=executor, lock=lock, last_used=time.monotonic())
        self._sessions[key] = session
        return session

    def end(self, device_serial: str | None) -> bool:
        """Releases the device lock and drops the session. Idempotent."""
        self._reap_idle()
        return self._end_key(self._key(device_serial))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/mcp_server/test_manual_session.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add mcp_server/manual_session.py tests/unit/mcp_server/test_manual_session.py
git commit -m "feat: add ManualSessionRegistry for host-agent-driven device sessions"
```

---

### Task 2: `mobile_observe` and `mobile_act` tools

**Files:**
- Create: `mcp_server/tools/manual_mode.py`
- Test: `tests/unit/mcp_server/test_manual_mode_tools.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/mcp_server/test_manual_mode_tools.py
"""mobile_observe / mobile_act / mobile_session_end, exercised against a mock device."""

import pytest

from artemis.mcp.action_executor import McpActionExecutor
from artemis.mcp.actuators.mock import MockActuator
from mcp_server.manual_session import ManualSessionRegistry
import mcp_server.tools.manual_mode as manual_mode


def _mock_executor_factory():
    """One MockActuator per device serial, wrapped in a real McpActionExecutor."""
    actuators: dict[str | None, MockActuator] = {}

    def factory(device_serial: str | None) -> McpActionExecutor:
        actuator = MockActuator()
        actuators[device_serial] = actuator
        return McpActionExecutor(actuator.ctx, actuator=actuator, agent_name="manual")

    factory.actuators = actuators
    return factory


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Each test gets its own registry/mock devices instead of the module singleton."""
    factory = _mock_executor_factory()
    registry = ManualSessionRegistry(executor_factory=factory)
    monkeypatch.setattr(manual_mode, "_registry", registry)
    yield registry
    for key in list(registry._sessions):
        registry.end(key)


@pytest.mark.asyncio
async def test_mobile_observe_returns_screenshot_and_elements():
    result = await manual_mode.mobile_observe(device_serial="dev1")
    assert result["status"] == "success"
    assert result["device_serial"] == "dev1"
    assert isinstance(result["elements_text"], str)
    assert result["width"] and result["height"]


@pytest.mark.asyncio
async def test_mobile_act_rejects_disallowed_action():
    result = await manual_mode.mobile_act(action="ask_explorer", args={}, device_serial="dev1")
    assert result["status"] == "error"
    assert "not available in manual mode" in result["error"]


@pytest.mark.asyncio
async def test_mobile_act_click_by_index_after_observe():
    observed = await manual_mode.mobile_observe(device_serial="dev1")
    assert observed["status"] == "success"

    # MockDeviceDriver starts on a screen with at least one indexed element.
    session = manual_mode._registry.get_or_create("dev1")
    assert session.indexed_elements, "mock device produced no elements to click"

    result = await manual_mode.mobile_act(
        action="click", args={"target": 1}, device_serial="dev1"
    )
    assert result["status"] == "success"
    assert result["device_serial"] == "dev1"


@pytest.mark.asyncio
async def test_mobile_act_invalid_index_surfaces_executor_error():
    await manual_mode.mobile_observe(device_serial="dev1")
    result = await manual_mode.mobile_act(
        action="click", args={"target": 9999}, device_serial="dev1"
    )
    assert result["status"] == "error"
    assert "Invalid target index" in result["message"]


@pytest.mark.asyncio
async def test_mobile_session_end_is_idempotent():
    await manual_mode.mobile_observe(device_serial="dev1")
    first = manual_mode.mobile_session_end(device_serial="dev1")
    second = manual_mode.mobile_session_end(device_serial="dev1")
    assert first == {"status": "success", "device_serial": "dev1", "ended": True}
    assert second == {"status": "success", "device_serial": "dev1", "ended": False}
```

Add the async test dependency if not already present (check first):

Run: `grep -n "pytest-asyncio\|asyncio_mode" pyproject.toml`

If `pytest-asyncio` / `asyncio_mode = "auto"` is already configured (it is used by other async MCP tool tests in this repo, e.g. `tests/unit/mcp/test_action_executor_*`), no change is needed; otherwise add `pytest.ini_options.asyncio_mode = "auto"` and `pytest-asyncio` to `pyproject.toml`'s test dependencies before continuing.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/mcp_server/test_manual_mode_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcp_server.tools.manual_mode'`

- [ ] **Step 3: Write the implementation**

```python
# mcp_server/tools/manual_mode.py
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""MCP Tools: mobile_observe, mobile_act, mobile_session_end.

Host-agent-driven "manual" mode: unlike mobile_run_task (Flash/Pro), these
tools perform no reasoning of their own and need no LLM credential configured
in Artemis. The calling MCP host supplies the intelligence by observing
(mobile_observe), deciding, and acting (mobile_act) in its own loop -- the
same reactive cycle FlashRunner runs autonomously, just driven turn-by-turn
by the host instead of Artemis's own LLM router.
"""

from typing import Any

from mcp_server.base import mcp
from mcp_server.manual_session import ManualSessionRegistry
from artemis.mcp.action_manifest import OPTIONAL_ACTIONS, REQUIRED_ACTIONS
from artemis.runtime.device_lock import DeviceBusyError

__all__ = ["mobile_observe", "mobile_act", "mobile_session_end"]

#: Device actions exposed in manual mode -- Flash's exact action vocabulary.
#: ask_explorer/video_analyzer/note/history tools are intentionally excluded:
#: they invoke Artemis's own LLM router internally, which would silently
#: reintroduce the credential requirement this mode exists to remove.
_ALLOWED_ACTIONS: frozenset[str] = REQUIRED_ACTIONS | OPTIONAL_ACTIONS

_registry = ManualSessionRegistry()


@mcp.tool()
async def mobile_observe(device_serial: str | None = None) -> dict[str, Any]:
    """Captures the current screen and numbered UI-element list for host-driven control.

    Unlike mobile_run_task, this performs no reasoning and needs no LLM
    credential configured in Artemis: the calling MCP host supplies the
    intelligence by observing (this tool), deciding, and acting (mobile_act)
    in its own loop. Call mobile_session_end when done so other Artemis tasks
    can use the device.

    Args:
        device_serial: Optional device serial to target; omitted selects the
          default connected device.
    """
    try:
        session = _registry.get_or_create(device_serial)
    except DeviceBusyError as e:
        return {"status": "error", "error": str(e)}

    try:
        action_session = await session.executor._session_or_start()
        obs = await action_session.observe(settle_ms=0)
    except Exception as e:
        return {"status": "error", "error": f"Failed to observe device: {e}"}

    if not obs.ok:
        return {"status": "error", "error": obs.message}

    session.indexed_elements = obs.elements
    session.indexed_points = [el["center"] for el in obs.elements]
    session.latest_screenshot = obs.screenshot_path

    return {
        "status": "success",
        "device_serial": device_serial or "auto-select",
        "screenshot": obs.screenshot_path,
        "elements_text": obs.elements_text,
        "width": obs.width,
        "height": obs.height,
    }


@mcp.tool()
async def mobile_act(
    action: str,
    args: dict[str, Any] | None = None,
    device_serial: str | None = None,
) -> dict[str, Any]:
    """Runs one validated device action and returns the resulting screen state.

    Args:
        action: One of: click, long_press, input_text, click_sequence, swipe,
          press_key, manage_app, wait_for_delay, wait_for_text, open_link,
          erase_one_char, focus_and_clear_text. A click/long_press/input_text
          'target' may be an element index from the last mobile_observe or
          mobile_act call's elements_text list, or a normalized [x, y] pair
          (in which case args must also include 'target_description').
          ask_explorer, video_analyzer, and the note/history tools are not
          available here: they invoke Artemis's own LLM and would silently
          reintroduce the credential requirement this mode avoids.
        args: Action-specific arguments, e.g. {"target": 3} for click, or
          {"text": "hello", "target": 2} for input_text. Defaults to {}.
        device_serial: Optional device serial to target; omitted selects the
          default connected device.
    """
    if action not in _ALLOWED_ACTIONS:
        return {
            "status": "error",
            "error": (
                f"Action '{action}' is not available in manual mode. Allowed: "
                + ", ".join(sorted(_ALLOWED_ACTIONS))
            ),
        }

    try:
        session = _registry.get_or_create(device_serial)
    except DeviceBusyError as e:
        return {"status": "error", "error": str(e)}

    result = await session.executor.execute(
        action,
        args or {},
        tool_call_id="manual",
        state=session,
    )

    return {
        "status": result.status,
        "message": result.text_summary,
        "device_serial": device_serial or "auto-select",
        "screenshot": session.latest_screenshot,
        "elements_text": result.ui_elements_text,
    }


@mcp.tool()
def mobile_session_end(device_serial: str | None = None) -> dict[str, Any]:
    """Releases the device lock and ends a manual mobile_observe/mobile_act session.

    Idempotent: ending an already-ended or nonexistent session is not an error.

    Args:
        device_serial: Optional device serial whose session to end; omitted
          targets the default-keyed session.
    """
    ended = _registry.end(device_serial)
    return {
        "status": "success",
        "device_serial": device_serial or "auto-select",
        "ended": ended,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/mcp_server/test_manual_mode_tools.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add mcp_server/tools/manual_mode.py tests/unit/mcp_server/test_manual_mode_tools.py
git commit -m "feat: add mobile_observe/mobile_act/mobile_session_end MCP tools"
```

---

### Task 3: Wire the new tools into the server

**Files:**
- Modify: `mcp_server/tools/__init__.py`
- Modify: `mcp_server/base.py`

- [ ] **Step 1: Register the tools**

Edit `mcp_server/tools/__init__.py`:

```python
"""MCP Tools package for ARTEMIS."""

from mcp_server.tools.device_state import mobile_get_device_state
from mcp_server.tools.diagnose import mobile_diagnose
from mcp_server.tools.inspect_trace import mobile_inspect_trace
from mcp_server.tools.manual_mode import mobile_act, mobile_observe, mobile_session_end
from mcp_server.tools.task_manager import mobile_manage_task
from mcp_server.tools.task_runner import mobile_run_task

__all__ = [
    "mobile_run_task",
    "mobile_manage_task",
    "mobile_get_device_state",
    "mobile_inspect_trace",
    "mobile_diagnose",
    "mobile_observe",
    "mobile_act",
    "mobile_session_end",
]
```

- [ ] **Step 2: Update the shared server instructions**

Edit `mcp_server/base.py`, extending the `instructions` string:

```python
mcp = FastMCP(
    "artemis",
    instructions=(
        "ARTEMIS is an autonomous mobile AI agent and Android UI automation engine. "
        "Use mobile_run_task to launch autonomous UI workflows on connected Android devices or emulators, "
        "mobile_manage_task to check status or steer execution, "
        "mobile_get_device_state to inspect real-time device screen/hierarchy, "
        "mobile_inspect_trace to inspect detailed execution steps and visual action overlays, "
        "mobile_diagnose whenever a tool errors, no device is found, or the user says "
        "ARTEMIS is not working: it checks the environment and returns ordered fix steps, "
        "and mobile_observe / mobile_act / mobile_session_end for host-agent-driven manual "
        "mode: when the calling MCP host has its own model and no Artemis LLM credential is "
        "configured, it can observe the screen and issue one device action at a time itself "
        "instead of delegating to mobile_run_task."
    ),
)
```

- [ ] **Step 3: Verify the server still imports cleanly**

Run: `uv run python -c "import mcp_server.tools; print(sorted(mcp_server.tools.__all__))"`
Expected: prints the list including `'mobile_act'`, `'mobile_observe'`, `'mobile_session_end'` with no import errors.

- [ ] **Step 4: Run the full unit suite**

Run: `uv run pytest`
Expected: PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add mcp_server/tools/__init__.py mcp_server/base.py
git commit -m "feat: register manual-mode tools on the shared FastMCP server"
```

---

### Task 4: Lint and typecheck

**Files:** none new — validation only.

- [ ] **Step 1: Run lint**

Run: `make lint`
Expected: no errors. Fix any `mcp_server/manual_session.py` / `mcp_server/tools/manual_mode.py` findings (unused imports, line length) directly in those files.

- [ ] **Step 2: Run typecheck**

Run: `make typecheck`
Expected: no errors. If `McpActionExecutor._session_or_start` or `ActionSession.observe` are flagged as accessing a private/underscored member across modules, add a narrow `# type: ignore[...]` on that exact line with no broader suppression, matching the specific error pyright reports.

- [ ] **Step 3: Commit if any fixes were needed**

```bash
git add -u
git commit -m "fix: satisfy lint/typecheck for manual-mode tools"
```

(Skip this step entirely if lint/typecheck passed clean in Steps 1-2 — no empty commit.)

---

### Task 5: Device end-to-end test and README

**Files:**
- Create: `tests/e2e/test_manual_mode_device.py`
- Modify: `README.md`

- [ ] **Step 1: Write the device-marked e2e test**

```python
# tests/e2e/test_manual_mode_device.py
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""End-to-end manual-mode cycle against a real attached device.

Run explicitly via `make test-device` (never part of the default `make test`
suite): requires an attached, authorized Android device or emulator.
"""

import pytest

from artemis.runtime.device_lock import DeviceExecutionLock
import mcp_server.tools.manual_mode as manual_mode


@pytest.mark.android
@pytest.mark.asyncio
async def test_observe_act_observe_cycle_releases_lock_on_session_end():
    first = await manual_mode.mobile_observe()
    assert first["status"] == "success"
    assert first["elements_text"]

    device_serial = first["device_serial"]
    session = manual_mode._registry.get_or_create(
        None if device_serial == "auto-select" else device_serial
    )
    assert session.indexed_elements, "no elements observed on the live device's home screen"

    act_result = await manual_mode.mobile_act(action="press_key", args={"key": "HOME"})
    assert act_result["status"] == "success"

    second = await manual_mode.mobile_observe()
    assert second["status"] == "success"

    end_result = manual_mode.mobile_session_end()
    assert end_result["ended"] is True

    # The device lock must be fully released: a fresh lock for the same
    # device must be acquirable immediately afterward.
    probe = DeviceExecutionLock(device_id="default", description="post-test probe")
    probe.acquire(blocking=False)
    probe.release()
```

- [ ] **Step 2: Run it against your connected device**

Run: `uv run pytest tests/e2e/test_manual_mode_device.py -v -m android`
Expected: PASS against an attached, authorized device (requires `adb devices` to show it as `device`, not `unauthorized`/`offline`).

- [ ] **Step 3: Document the new mode in the README**

Edit `README.md`: find the section listing `mobile_run_task` / `mobile_get_device_state` / `mobile_manage_task` (the MCP tools overview), and add:

```markdown
- **`mobile_observe` / `mobile_act` / `mobile_session_end`** — host-agent-driven
  manual mode. Unlike `mobile_run_task`, these tools perform no reasoning of
  their own and require no LLM credential configured in Artemis: the calling
  MCP host (Claude Code, Codex, Antigravity, or any other host with its own
  model) observes the screen, decides the next step itself, and issues it
  through `mobile_act` — the same reactive loop Flash runs autonomously, just
  driven turn-by-turn by the host. See
  `docs/superpowers/specs/2026-09-16-host-agent-manual-mode-design.md` for the
  full design.
```

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_manual_mode_device.py README.md
git commit -m "test: add device e2e coverage and README docs for manual mode"
```

---

## Self-Review

**Spec coverage:**
- Reuse `observe()`-equivalent capture + indexed elements — Task 2 (`mobile_observe` via `ActionSession.observe`). ✓
- Reuse `McpActionExecutor` for actions — Task 2 (`mobile_act`). ✓
- `DeviceExecutionLock` integration / contention — Task 1. ✓
- Idle-timeout reaping — Task 1 (`_reap_idle`, tested). ✓
- Excluded tool set (`ask_explorer`, etc.) — Task 2 (`_ALLOWED_ACTIONS` restricted to `REQUIRED_ACTIONS | OPTIONAL_ACTIONS`, tested). ✓
- Explicit session end, idempotent — Task 2 (`mobile_session_end`, tested). ✓
- Unit tests with mock actuator — Task 1 & 2. ✓
- Device-marked e2e test — Task 5. ✓
- README documentation — Task 5. ✓
- Open question (idle timeout default) — resolved: `DEFAULT_IDLE_TIMEOUT_S = 600.0`, matching the spec's proposed default.
- Open question (`manage_app` install) — resolved per spec: launch/stop only in this cut; `app_path` install is out of scope and not implemented.

**Placeholder scan:** none found — every step has complete, concrete code.

**Type consistency:** `ManualSession` field names (`indexed_elements`, `indexed_points`, `latest_screenshot`, `latest_ui_hierarchy`) match exactly what `McpActionExecutor._execute_device_action`/`_resolve_index`/`_translate_swipe` read and write (verified against `artemis/mcp/action_executor.py` during design). `ManualSessionRegistry.get_or_create`/`.end` signatures used identically in both tool functions and both test files.
