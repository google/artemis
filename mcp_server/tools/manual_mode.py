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
    except Exception as e:
        return {"status": "error", "error": f"Failed to initialize/lock Android device controller: {e}"}

    try:
        action_session = await session.executor._session_or_start()
        obs = await action_session.observe(settle_ms=0)
    except Exception as e:
        return {"status": "error", "error": f"Failed to observe device: {e}"}

    if not obs.ok:
        return {"status": "error", "error": obs.message}

    # obs.hierarchy_ok is False when the screenshot succeeded but the UI
    # hierarchy parse failed; the previous element index must survive that so
    # a later click-by-index against last-known-good elements still resolves.
    if obs.hierarchy_ok:
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
    except Exception as e:
        return {"status": "error", "error": f"Failed to initialize/lock Android device controller: {e}"}

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
