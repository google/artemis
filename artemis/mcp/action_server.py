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
from artemis.mcp.action_specs import (
    exception_prefix,
    make_wire_handler,
    wire_dialects,
)
from artemis.mcp.action_types import ActionCode, ActionResult, ObserveResult
from artemis.mcp.actuators.base import Actuator
from artemis.mcp.observation import observe as observe_impl
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["build_action_server"]


def _wrap(res: ActionResult) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=res.message)],
        structuredContent=res.model_dump(mode="json"),
        isError=False,
    )


def _wrap_exception(action: str, exc: Exception) -> CallToolResult:
    return _wrap(
        ActionResult.failure(action, f"{exception_prefix(action)}: {exc}", detail=repr(exc))
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
    # Declarations and argument conversion both come from the canonical manifest
    # (artemis/mcp/action_specs.py); this server contributes only the transport
    # wrapping, so the served schema cannot drift from the manifest.

    for spec in wire_dialects():
        if spec.name in caps:
            mcp.add_tool(
                make_wire_handler(spec, actuator, _wrap, _wrap_exception),
                name=spec.name,
                description=spec.wire.description,
            )

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
                content=[
                    TextContent(type="text", text="screenshot" if payload["ok"] else str(payload))
                ],
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
                payload = {
                    "ok": False,
                    "error": f"ui hierarchy fetch timed out after {timeout_ms}ms",
                }
            except Exception as e:
                payload = {"ok": False, "error": str(e)}
            return CallToolResult(
                content=[
                    TextContent(type="text", text="ui_hierarchy" if payload["ok"] else str(payload))
                ],
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
