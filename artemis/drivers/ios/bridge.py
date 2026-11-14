# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Task-safe access to Xcode's MCP bridge using Artemis's existing MCP SDK.

The SDK handles initialization, notifications and response IDs. A single owner
task enters and exits the SDK's AnyIO contexts, and serializes device commands.
A timeout retires the bridge; a possibly executed input is never retried.
"""

import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.shared.exceptions import McpError

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


_APPROVAL_MESSAGES = (
    "agent isn't approved",
    "agent is not approved",
    "waiting for the user to approve",
)


class XcodeApprovalRequiredError(RuntimeError):
    """Xcode refused a tool because this interpreter lacks agent/folder approval."""

    def __init__(
        self,
        tool_name: str,
        native_message: str,
        *,
        workspace_path: str | Path | None = None,
    ):
        super().__init__(f"Xcode tool {tool_name} failed: {native_message}")
        self.tool_name = tool_name
        self.native_message = native_message
        self.workspace_path = (
            Path(workspace_path).expanduser().resolve() if workspace_path is not None else None
        )

    @property
    def guidance(self) -> str:
        """Human steps to approve this interpreter; never executes them."""
        return xcode_approval_guidance(self.workspace_path)


def xcode_approval_guidance(workspace_path: str | Path | None = None) -> str:
    """Human-facing Xcode approval steps; printing is the only action taken."""
    workspace = str(workspace_path) if workspace_path else "not supplied"
    return (
        "Xcode approval is required for the Python interpreter running Artemis.\n"
        f"Interpreter: {Path(sys.executable).resolve()}\n"
        f"Workspace: {workspace}\n\n"
        "Open the Xcode MCP menu bar icon and approve this interpreter and the selected "
        "project folder. Choose Always Allow or persistent approval there if offered. "
        "Alternatively, inspect the relevant pending request IDs and approve only those "
        "entries from your own terminal:\n"
        "  xcrun mcp-server status\n"
        "  sudo xcrun mcp-server approve <REQUEST-ID> --always\n\n"
        "Artemis never runs sudo or changes global access settings. After approval, "
        "rerun the task. Interpreter upgrades, a different project folder, or expiring "
        "grants can require approval again.\n"
        "If Xcode has not recorded a request yet, rerun with "
        "--platform ios --ios-workspace /absolute/path/YourApp.xcodeproj "
        "(or pass workspace_path to for_ios_device())."
    )


_ERRORS = (
    OSError,
    ValueError,
    RuntimeError,
    McpError,
    ExceptionGroup,
    anyio.EndOfStream,
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
)


class XcodeBridge:
    """Own one native bridge subprocess for one simulator driver."""

    def __init__(self, timeout_seconds: float = 120.0):
        self.timeout_seconds = timeout_seconds
        self._owner: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[set[str]] | None = None
        self._requests: asyncio.Queue[Any] = asyncio.Queue()
        self._active: asyncio.Future[Any] | None = None
        self._start_lock = asyncio.Lock()
        self.tools: set[str] = set()

    @property
    def connected(self) -> bool:
        return self._owner is not None and not self._owner.done()

    async def start(self) -> None:
        async with self._start_lock:
            if self._owner is not None and not self._owner.done():
                return
            self._ready = asyncio.get_running_loop().create_future()
            self._owner = asyncio.create_task(self._run(), name="artemis-xcode-bridge")
            try:
                self.tools = await asyncio.wait_for(
                    asyncio.shield(self._ready), self.timeout_seconds
                )
            except (*_ERRORS, TimeoutError, asyncio.CancelledError):
                await self.close()
                raise

    async def _run(self) -> None:
        try:
            # mcpbridge only needs a clean base env plus the Xcode pinning
            # vars — do not forward the full environment (API keys) to a
            # long-lived child process.
            env = get_default_environment()
            for key in ("DEVELOPER_DIR", "MCP_XCODE_PID"):
                if value := os.environ.get(key):
                    env[key] = value
            params = StdioServerParameters(command="xcrun", args=["mcpbridge"], env=env)
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    names: set[str] = set()
                    cursor = None
                    while True:
                        page = await session.list_tools(cursor=cursor)
                        names.update(tool.name for tool in page.tools)
                        cursor = page.nextCursor
                        if not cursor:
                            break
                    if self._ready is not None and not self._ready.done():
                        self._ready.set_result(names)
                    while True:
                        request = await self._requests.get()
                        if request is None:
                            return
                        future, name, arguments = request
                        if future.cancelled():
                            continue
                        self._active = future
                        result = await session.call_tool(name, arguments)
                        if not future.done():
                            future.set_result(result)
                        self._active = None
        except _ERRORS as exc:
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(RuntimeError(f"Cannot connect to Xcode MCP: {exc}"))
            if self._active is not None and not self._active.done():
                self._active.set_exception(RuntimeError(f"Xcode MCP connection failed: {exc}"))
        finally:
            error = RuntimeError(
                "Xcode MCP bridge has closed. Reconnect the driver before retrying."
            )
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(error)
            if self._active is not None and not self._active.done():
                self._active.set_exception(error)
            self._active = None
            while not self._requests.empty():
                request = self._requests.get_nowait()
                if request is not None and not request[0].done():
                    request[0].set_exception(error)

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._owner is None or self._owner.done():
            raise RuntimeError("Xcode MCP bridge is not connected.")
        future = asyncio.get_running_loop().create_future()
        self._requests.put_nowait((future, name, arguments))
        try:
            result = await asyncio.wait_for(future, self.timeout_seconds)
        except (TimeoutError, asyncio.CancelledError):
            await self.close()
            raise
        if result.isError:
            message = " ".join(block.text for block in result.content if block.type == "text")
            if any(marker in message.casefold() for marker in _APPROVAL_MESSAGES):
                raise XcodeApprovalRequiredError(name, message)
            raise RuntimeError(f"Xcode tool {name} failed: {message}")
        if isinstance(result.structuredContent, dict):
            return result.structuredContent
        # Xcode also supports older clients with JSON text content.
        for block in result.content:
            if block.type == "text":
                try:
                    data = json.loads(block.text)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    return data
        raise RuntimeError(f"Xcode tool {name} returned no structured result.")

    async def close(self) -> None:
        owner, self._owner = self._owner, None
        if owner is None:
            return
        if not owner.done():
            owner.cancel()
        try:
            await owner
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            # Owner failures already propagate to requesters via futures; a
            # close() must never mask the error its caller is handling.
            logger.debug(f"Xcode bridge owner exited with an error: {exc}")
        if self._ready is not None and self._ready.done() and not self._ready.cancelled():
            self._ready.exception()  # consume an initialization error on cancellation
        self.tools.clear()
