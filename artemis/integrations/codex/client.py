"""Small stdio client for the official Codex app-server protocol.

Authentication stays inside Codex: never read auth.json, extract tokens, or
send subscription credentials to an OpenAI-compatible API endpoint.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any


class CodexError(RuntimeError):
    """An actionable Codex installation, authentication, or protocol error."""


class CodexClient:
    """One owned app-server process; requests are intentionally serialized."""

    def __init__(self, cwd: Path, executable: str = "codex", timeout: float = 30):
        self.cwd = cwd
        self.executable = executable
        self.timeout = timeout
        self.process: asyncio.subprocess.Process | None = None
        self._stderr: asyncio.Task | None = None
        self._next_id = 0
        self._events: deque[dict[str, Any]] = deque()

    async def __aenter__(self) -> CodexClient:
        executable = shutil.which(self.executable)
        if not executable:
            raise CodexError("Codex CLI not found. Install Codex and put its executable on PATH.")
        # Avoid accidental API billing when this explicit ChatGPT integration is used.
        env = os.environ.copy()
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            env.pop(key, None)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = await asyncio.create_subprocess_exec(
            executable,
            "app-server",
            "--listen",
            "stdio://",
            cwd=self.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
            creationflags=flags,
        )
        self._stderr = asyncio.create_task(self._drain_stderr())
        initialized = False
        try:
            await self.request(
                "initialize",
                {
                    "clientInfo": {"name": "artemis", "version": "1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self.send({"method": "initialized", "params": {}})
            initialized = True
            return self
        finally:
            if not initialized:
                await self.close()

    async def __aexit__(self, *_args) -> None:
        await self.close()

    async def _drain_stderr(self) -> None:
        assert self.process and self.process.stderr
        # Do not persist authentication diagnostics or unbounded subprocess logs.
        while await self.process.stderr.read(8192):
            pass

    async def close(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self._stderr:
            self._stderr.cancel()
            with suppress(asyncio.CancelledError):
                await self._stderr

    async def send(self, payload: dict[str, Any]) -> None:
        assert self.process and self.process.stdin
        try:
            self.process.stdin.write((json.dumps(payload) + "\n").encode())
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise CodexError("Codex app-server closed its input stream.") from exc

    async def _read(self) -> dict[str, Any]:
        assert self.process and self.process.stdout
        line = await self.process.stdout.readline()
        if not line:
            raise CodexError("Codex app-server exited before completing the request.")
        try:
            value = json.loads(line)
        except (ValueError, UnicodeDecodeError) as exc:
            raise CodexError("Invalid JSON from Codex app-server.") from exc
        if not isinstance(value, dict):
            raise CodexError("Invalid message from Codex app-server.")
        return value

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        await self.send({"id": request_id, "method": method, "params": params})
        async with asyncio.timeout(self.timeout):
            while True:
                message = await self._read()
                # Server requests have IDs too. Never mistake one for our reply.
                if "method" not in message and message.get("id") == request_id:
                    if "error" in message:
                        raise CodexError(
                            f"Codex {method}: {message['error'].get('message', 'failed')}"
                        )
                    return message.get("result", {})
                self._events.append(message)

    async def event(self) -> dict[str, Any]:
        return self._events.popleft() if self._events else await self._read()

    async def reject_request(self, message: dict[str, Any]) -> None:
        await self.send(
            {
                "id": message["id"],
                "error": {"code": -32601, "message": "Unsupported by the ARTEMIS Codex client"},
            }
        )

    async def account(self) -> dict[str, Any]:
        response = await self.request("account/read", {"refreshToken": False})
        account = response.get("account") or {}
        # Deliberately expose no email, account identifier, or credentials.
        return {"authenticated": account.get("type") == "chatgpt", "auth_type": account.get("type")}

    async def require_chatgpt(self) -> None:
        if not (await self.account())["authenticated"]:
            raise CodexError("ChatGPT login required. Run `artemis codex login` first.")
