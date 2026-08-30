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

"""The unified device action MCP server.

``build_action_server`` turns an actuator into a ``FastMCP`` server that registers
**only** the tools the actuator implements, plus its extension tools -- so
``list_tools()`` is clean by construction and an absent optional tool is invisible to
every consumer.

Return protocol: every device action returns a ``CallToolResult`` whose
``structuredContent`` is an ``ActionResult`` -- ``ok``/``code`` carry the real status
(killing the historical ``"Error" in text`` sniffing) while ``content`` keeps the
historical human-readable message. ``isError`` is reserved for protocol-level
failures; a device refusing an action is ``isError=False, ok=False`` so it reaches the
model as an observation, not an exception.

Exceptions raised by the actuator are wrapped here with the historical
``"Error during X: {e}"`` wording the agents' transcripts already contain.
"""

import asyncio
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent

from artemis.mcp.action_manifest import validate_actuator
from artemis.mcp.action_types import ActionCode, ActionResult, ObserveResult
from artemis.mcp.actuators.base import Actuator
from artemis.mcp.observation import observe as observe_impl
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["build_action_server"]

# Exception wrapping per tool; wording matches the historical executor `except` arms.
_EXCEPTION_PREFIX = {
    "click": "Error during click",
    "click_sequence": "Error executing click sequence",
    "long_press": "Error during long press",
    "input_text": "Error during input text",
    "swipe": "Error during swipe",
    "press_key": "Error during press_key",
    "manage_app": "Error during manage_app",
    "wait_for_delay": "Error during wait_for_delay",
    "wait_for_text": "Error during wait_for_text",
}


def _wrap(res: ActionResult) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=res.message)],
        structuredContent=res.model_dump(mode="json"),
        isError=False,
    )


def _wrap_exception(action: str, exc: Exception) -> CallToolResult:
    prefix = _EXCEPTION_PREFIX.get(action, f"Error during {action}")
    return _wrap(
        ActionResult.failure(action, f"{prefix}: {exc}", detail=repr(exc))
    )


def build_action_server(actuator: Actuator, name: str = "artemis_actions") -> FastMCP:
    """Builds a FastMCP server exposing exactly what ``actuator`` implements.

    Raises ``ActuatorContractError`` when the actuator violates the manifest, so a
    misconfigured backend fails before serving anything.
    """
    validate_actuator(actuator)
    caps = actuator.capabilities()
    mcp = FastMCP(name)

    # --- Device actions (registered only when implemented) ---------------------------

    if "click" in caps:

        @mcp.tool()
        async def click(
            target: list[int], times: int = 1, delay_ms: int = 100
        ) -> Annotated[CallToolResult, ActionResult]:
            """Tap at a 0-1000 normalized [x, y] coordinate."""
            try:
                return _wrap(
                    await actuator.click(
                        int(target[0]), int(target[1]), times=times, delay_ms=delay_ms
                    )
                )
            except Exception as e:
                return _wrap_exception("click", e)

    if "click_sequence" in caps:

        @mcp.tool()
        async def click_sequence(
            sequence: list[list[int]], delay_ms: int = 50
        ) -> Annotated[CallToolResult, ActionResult]:
            """Tap a series of 0-1000 normalized [x, y] points in one atomic burst."""
            try:
                points = [(int(p[0]), int(p[1])) for p in sequence]
                return _wrap(await actuator.click_sequence(points, delay_ms=delay_ms))
            except Exception as e:
                return _wrap_exception("click_sequence", e)

    if "long_press" in caps:

        @mcp.tool()
        async def long_press(
            target: list[int], duration_ms: int = 1000
        ) -> Annotated[CallToolResult, ActionResult]:
            """Long-press at a 0-1000 normalized [x, y] coordinate."""
            try:
                return _wrap(
                    await actuator.long_press(
                        int(target[0]), int(target[1]), duration_ms=duration_ms
                    )
                )
            except Exception as e:
                return _wrap_exception("long_press", e)

    if "input_text" in caps:

        @mcp.tool()
        async def input_text(
            text: str,
            target: list[int] | None = None,
            clear_exist: bool = True,
        ) -> Annotated[CallToolResult, ActionResult]:
            """Type text, optionally focusing a 0-1000 normalized [x, y] target first."""
            try:
                norm = (int(target[0]), int(target[1])) if target else None
                return _wrap(
                    await actuator.input_text(text, norm, clear_exist=clear_exist)
                )
            except Exception as e:
                return _wrap_exception("input_text", e)

    if "swipe" in caps:

        @mcp.tool()
        async def swipe(
            start: list[int], end: list[int], duration_ms: int = 800
        ) -> Annotated[CallToolResult, ActionResult]:
            """Swipe between two 0-1000 normalized [x, y] points."""
            try:
                return _wrap(
                    await actuator.swipe(
                        (int(start[0]), int(start[1])),
                        (int(end[0]), int(end[1])),
                        duration_ms,
                    )
                )
            except Exception as e:
                return _wrap_exception("swipe", e)

    if "press_key" in caps:

        @mcp.tool()
        async def press_key(key: str) -> Annotated[CallToolResult, ActionResult]:
            """Press a device key (home, back, enter, delete, tab, search, menu, app_switch)."""
            try:
                return _wrap(await actuator.press_key(key))
            except Exception as e:
                return _wrap_exception("press_key", e)

    if "manage_app" in caps:

        @mcp.tool()
        async def manage_app(
            action: str, app_name: str
        ) -> Annotated[CallToolResult, ActionResult]:
            """Launch or stop an app by human-readable name or package."""
            try:
                return _wrap(await actuator.manage_app(action, app_name))
            except Exception as e:
                return _wrap_exception("manage_app", e)

    if "wait_for_delay" in caps:

        @mcp.tool()
        async def wait_for_delay(
            time_in_ms: int,
        ) -> Annotated[CallToolResult, ActionResult]:
            """Wait for a fixed number of milliseconds."""
            try:
                return _wrap(await actuator.wait_for_delay(time_in_ms))
            except Exception as e:
                return _wrap_exception("wait_for_delay", e)

    if "wait_for_text" in caps:

        @mcp.tool()
        async def wait_for_text(
            text: str, wait_state: str = "appear", timeout_ms: int = 5000
        ) -> Annotated[CallToolResult, ActionResult]:
            """Wait for text to appear on or disappear from the screen."""
            try:
                return _wrap(await actuator.wait_for_text(text, wait_state, timeout_ms))
            except Exception as e:
                return _wrap_exception("wait_for_text", e)

    if "open_link" in caps:

        @mcp.tool()
        async def open_link(url: str) -> Annotated[CallToolResult, ActionResult]:
            """Open a URL on the device."""
            try:
                return _wrap(await actuator.open_link(url))
            except Exception as e:
                return _wrap_exception("open_link", e)

    if "erase_one_char" in caps:

        @mcp.tool()
        async def erase_one_char() -> Annotated[CallToolResult, ActionResult]:
            """Erase a single character in the focused field."""
            try:
                return _wrap(await actuator.erase_one_char())
            except Exception as e:
                return _wrap_exception("erase_one_char", e)

    if "focus_and_clear_text" in caps:

        @mcp.tool()
        async def focus_and_clear_text(
            target: list[int],
        ) -> Annotated[CallToolResult, ActionResult]:
            """Focus the field at a 0-1000 normalized [x, y] and clear its text."""
            try:
                return _wrap(
                    await actuator.focus_and_clear_text(int(target[0]), int(target[1]))
                )
            except Exception as e:
                return _wrap_exception("focus_and_clear_text", e)

    # --- Internal observation tools (never declared to an LLM) -----------------------

    if "observe_screen" in caps:

        @mcp.tool()
        async def observe_screen(
            settle_ms: int = 400, include_image: bool = False
        ) -> Annotated[CallToolResult, ObserveResult]:
            """Capture and index the current screen (adapter-internal)."""
            try:
                obs, img_bytes = await observe_impl(
                    getattr(actuator, "ctx", None),
                    actuator.controller,
                    settle_ms=settle_ms,
                )
                content: list[Any] = [TextContent(type="text", text=obs.message)]
                if include_image and img_bytes:
                    import base64 as _b64

                    content.append(
                        ImageContent(
                            type="image",
                            data=_b64.b64encode(img_bytes).decode("utf-8"),
                            mimeType="image/jpeg",
                        )
                    )
                return CallToolResult(
                    content=content,
                    structuredContent=obs.model_dump(mode="json"),
                    isError=False,
                )
            except Exception as e:
                obs = ObserveResult(
                    ok=False,
                    code=ActionCode.DEVICE_ERROR,
                    message=f"Error during observe_screen: {e}",
                    hierarchy_ok=False,
                )
                return CallToolResult(
                    content=[TextContent(type="text", text=obs.message)],
                    structuredContent=obs.model_dump(mode="json"),
                    isError=False,
                )

    # Timeouts on the two observation tools run INSIDE the tool, around the actuator
    # call only. They must never be expressed as a transport-level read timeout
    # (``read_timeout_seconds``) or a caller-side ``asyncio.wait_for``: both produce a
    # late response to an abandoned request, which crashes the mcp-1.29 client
    # receive loop (``_handle_response`` sends into the request's already-closed
    # response stream) and bricks the in-memory session for every later caller.
    # A server-side timeout yields a normal error payload and no late response.

    if "take_screenshot" in caps:

        @mcp.tool()
        async def take_screenshot(timeout_ms: int | None = None) -> CallToolResult:
            """Return the current screen as base64 (adapter-internal, no persistence)."""
            try:
                coro = actuator.take_screenshot()
                if timeout_ms:
                    coro = asyncio.wait_for(coro, timeout=timeout_ms / 1000.0)
                b64 = await coro
                payload: dict[str, Any] = {"ok": True, "image_b64": b64}
            except TimeoutError:
                payload = {"ok": False, "error": f"screenshot timed out after {timeout_ms}ms"}
            except Exception as e:
                payload = {"ok": False, "error": str(e)}
            return CallToolResult(
                content=[TextContent(type="text", text="screenshot" if payload["ok"] else str(payload))],
                structuredContent=payload,
                isError=False,
            )

    if "get_ui_hierarchy" in caps:

        @mcp.tool()
        async def get_ui_hierarchy(timeout_ms: int | None = None) -> CallToolResult:
            """Return the raw UI element hierarchy (adapter-internal)."""
            try:
                coro = actuator.get_ui_elements()
                if timeout_ms:
                    coro = asyncio.wait_for(coro, timeout=timeout_ms / 1000.0)
                elements = await coro
                payload: dict[str, Any] = {"ok": True, "elements": elements}
            except TimeoutError:
                payload = {"ok": False, "error": f"ui hierarchy fetch timed out after {timeout_ms}ms"}
            except Exception as e:
                payload = {"ok": False, "error": str(e)}
            return CallToolResult(
                content=[TextContent(type="text", text="ui_hierarchy" if payload["ok"] else str(payload))],
                structuredContent=payload,
                isError=False,
            )

    # --- Backend extension tools -----------------------------------------------------

    for ext in actuator.extensions():
        mcp.add_tool(ext.handler, name=ext.name, description=ext.description)

    logger.info(
        f"Action server '{name}' built with {len(caps)} device actions and"
        f" {len(actuator.extensions())} extension tools."
    )
    return mcp
