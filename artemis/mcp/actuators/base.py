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

"""The actuator protocol: what any backend must look like to drive a device.

An actuator answers exactly one question -- *how to perform a physical action* -- in
the canonical 0-1000 normalized coordinate space. It knows nothing about LangGraph
``State``, prompts, agents, screenshots-as-observations, or MCP. Pixel geometry (or a
robot arm's motion planning) is a private detail behind this boundary.

A backend advertises what it can do through :meth:`Actuator.capabilities` (names from
``artemis.mcp.action_manifest.DEVICE_ACTIONS``) and may contribute tools nobody
anticipated through :meth:`Actuator.extensions`. ``validate_actuator`` in the manifest
enforces the contract at session start.

Methods for actions a backend does not implement should simply not exist (or raise
``NotImplementedError``); callers consult ``capabilities()`` first.
"""

from typing import Any, Protocol, runtime_checkable

from artemis.mcp.action_manifest import ExtensionTool
from artemis.mcp.action_types import ActionResult

__all__ = ["Actuator"]


@runtime_checkable
class Actuator(Protocol):
    """Protocol every actuator backend implements.

    All coordinates are 0-1000 normalized. Every action coroutine returns an
    :class:`ActionResult` and must not raise for device-level failures -- those are
    reported through ``ok=False`` so a failed tap reaches the agent as an observation
    rather than an exception.
    """

    def capabilities(self) -> frozenset[str]:
        """Names from ``DEVICE_ACTIONS`` this backend implements."""
        ...

    def extensions(self) -> list[ExtensionTool]:
        """Backend-specific tools unknown to the manifest."""
        ...

    # --- Required action -------------------------------------------------------------

    async def click_sequence(
        self, points: list[tuple[int, int]], delay_ms: int = 50
    ) -> ActionResult: ...

    # --- Optional actions ------------------------------------------------------------

    async def click(
        self, nx: int, ny: int, times: int = 1, delay_ms: int = 100
    ) -> ActionResult: ...

    async def long_press(self, nx: int, ny: int, duration_ms: int = 1000) -> ActionResult: ...

    async def input_text(
        self,
        text: str,
        target: tuple[int, int] | None = None,
        clear_exist: bool = True,
    ) -> ActionResult: ...

    async def swipe(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_ms: int = 800,
    ) -> ActionResult: ...

    async def press_key(self, key: str) -> ActionResult: ...

    async def manage_app(self, action: str, app_name: str) -> ActionResult: ...

    async def wait_for_delay(self, time_in_ms: int) -> ActionResult: ...

    async def wait_for_text(
        self, text: str, wait_state: str | None = None, timeout_ms: int | None = None
    ) -> ActionResult: ...

    async def open_link(self, url: str) -> ActionResult: ...

    async def erase_one_char(self) -> ActionResult: ...

    async def focus_and_clear_text(self, nx: int, ny: int) -> ActionResult: ...

    # --- Internal observation primitives ---------------------------------------------

    async def take_screenshot(self) -> str:
        """Returns the current screen as a base64-encoded JPEG/PNG string."""
        ...

    async def get_ui_elements(self) -> Any:
        """Returns the raw UI hierarchy elements as provided by the device."""
        ...

    async def get_screen_data(self) -> Any:
        """Returns combined screen data (screenshot + elements + dimensions)."""
        ...
