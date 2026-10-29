# Copyright 2025-2026 Minitap, Inc.
# Modifications Copyright 2026 Google LLC
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
#
# Originally from mobile-use (https://github.com/minitap-ai/mobile-use).
# See third_party/mobile_use/METADATA for the upstream source and local modifications.

"""Helpers for LLM calls, from upstream ``services/llm.py``.

``LLMWaitNotice`` waits for a call, tells the user when it is slow and enforces
a hard timeout. ``FallbackRunner`` retries a failed call on a fallback model.
``GetLLM`` is the type of ``get_llm``.

Artemis passes in its own behaviour (pause detection, error classification,
telemetry); this module does not import artemis at runtime.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AbstractContextManager, nullcontext
import logging
from typing import TYPE_CHECKING, Any, Literal, Protocol, overload

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

    from artemis.config import AgentNodeWithFallback, LLMUtilsNode, LLMUtilsNodeWithFallback
    from artemis.context import ArtemisContext

_logger = logging.getLogger(__name__)

WAITING_MESSAGE = "Waiting for LLM call response..."


class LLMWaitNotice:
    """Wait for LLM calls, report slow ones and enforce a hard timeout.

    Args:
        notify: Called with :data:`WAITING_MESSAGE` once a call outlives its notice delay.
        on_timeout: Called with the timeout message right before ``TimeoutError`` is raised.
        hold: While it returns ``True`` the hard-timeout clock is held at zero
            (e.g. the task is paused by the user).
        default_hard_timeout: Hard timeout in seconds when the call does not pass
            one; ``None`` waits indefinitely.
        poll_interval: Seconds between ``hold``/timeout checks after the notice.
    """

    def __init__(
        self,
        *,
        notify: Callable[[str], object],
        on_timeout: Callable[[str], object],
        hold: Callable[[], bool] = lambda: False,
        default_hard_timeout: float | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self._notify = notify
        self._on_timeout = on_timeout
        self._hold = hold
        self._default_hard_timeout = default_hard_timeout
        self._poll_interval = poll_interval

    async def __call__[T](
        self,
        llm_call: Coroutine[Any, Any, T],
        timeout_seconds: float = 10,
        hard_timeout: float | None = None,
    ) -> T:
        """Run ``llm_call``; show a notice after ``timeout_seconds``.

        The call is cancelled if the caller is cancelled or the hard timeout
        (counted from the start of the call) expires.
        """
        if hard_timeout is None:
            hard_timeout = self._default_hard_timeout
        llm_task = asyncio.create_task(llm_call)
        try:
            # asyncio.wait never raises on timeout and needs no timer task.
            done, _ = await asyncio.wait({llm_task}, timeout=timeout_seconds)
            if llm_task in done:
                return llm_task.result()

            self._notify(WAITING_MESSAGE)
            loop = asyncio.get_running_loop()
            budget = None if hard_timeout is None else max(0.0, hard_timeout - timeout_seconds)
            start_time = loop.time()
            while True:
                done, _ = await asyncio.wait({llm_task}, timeout=self._poll_interval)
                if llm_task in done:
                    return llm_task.result()
                if self._hold():
                    start_time = loop.time()
                    continue
                if budget is not None and loop.time() - start_time > budget:
                    message = f"LLM call timed out after {hard_timeout} seconds."
                    self._on_timeout(message)
                    raise TimeoutError(message)
        except BaseException:
            if not llm_task.done():
                llm_task.cancel()
            await asyncio.gather(llm_task, return_exceptions=True)
            raise


class FallbackRunner:
    """Runs a main LLM call and switches to a fallback call when it fails.

    The base class falls back on every ``Exception`` and on a ``None`` result.
    Subclasses narrow that down and report switches by overriding the hooks
    :meth:`main_scope`, :meth:`fallback_reason` and :meth:`on_fallback`.
    """

    def main_scope(self) -> AbstractContextManager[object]:
        """Context active while the main call runs; exited before the fallback starts."""
        return nullcontext()

    def fallback_reason(self, error: Exception) -> tuple[str, str | None] | None:
        """``(reason, category)`` if ``error`` should trigger the fallback, ``None`` to re-raise."""
        return "error", None

    def on_fallback(self, reason: str, category: str | None, error: str | None) -> None:
        """Report a switch to the fallback call."""
        detail = f": {error}" if error else ""
        _logger.warning(f"❗ Main LLM inference failed ({reason}{detail}). Falling back...")

    async def run[T](
        self,
        main_call: Callable[[], Awaitable[T]],
        fallback_call: Callable[[], Awaitable[T]],
        none_should_fallback: bool = True,
    ) -> T:
        try:
            with self.main_scope():
                result = await main_call()
        except Exception as e:
            decision = self.fallback_reason(e)
            if decision is None:
                raise
            self.on_fallback(decision[0], decision[1], str(e))
            return await fallback_call()

        if result is None and none_should_fallback:
            self.on_fallback("empty_result", None, None)
            return await fallback_call()
        return result


class GetLLM(Protocol):
    """Call signature of ``get_llm``: resolve the chat model configured for a node."""

    @overload
    def __call__(
        self,
        ctx: ArtemisContext,
        name: AgentNodeWithFallback,
        *,
        use_fallback: bool = False,
        temperature: float | None = None,
    ) -> BaseChatModel: ...

    @overload
    def __call__(
        self,
        ctx: ArtemisContext,
        name: LLMUtilsNode,
        *,
        is_utils: Literal[True],
        temperature: float | None = None,
    ) -> BaseChatModel: ...

    @overload
    def __call__(
        self,
        ctx: ArtemisContext,
        name: LLMUtilsNodeWithFallback,
        *,
        is_utils: Literal[True],
        use_fallback: bool = False,
        temperature: float | None = None,
    ) -> BaseChatModel: ...
