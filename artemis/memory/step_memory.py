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

"""Shared background step-summary runtime (StepMemoryService).

Hosts the scheduling skeleton lifted from the Flash VisualStepSummarizer:
zero-blocking dispatch, bounded retry, bounded flush, canonical step_id keying
with alias resolution (tool_call_id / legacy ordinals), and status queries.

What a summary *contains* (the lens semantics) lives in subclasses — the
service only guarantees how jobs are scheduled, retried, keyed, and drained.
Both Flash and (from M2 on) Pro profiles share this runtime.
"""

import asyncio
from typing import Any

from artemis.utils.logger import get_logger

logger = get_logger(__name__)

JobKey = int | str


class StepLens:
    """Formal lens interface: what one summary attempt *produces*.

    A lens turns one job payload into summary text; the surrounding service
    owns scheduling, retries, keying, and draining. Implementations must be
    side-effect-tolerant under retries (an attempt may run again after a
    failure) and return ``None``/raise to signal a failed attempt.

    Two lens altitudes exist in the redesign (§5): the step-level visual
    transition lens (:class:`~artemis.agents.flash.summarizer.VisualStepSummarizer`
    keeps it inlined as its ``_attempt`` for compatibility) and the chunk-level
    :class:`~artemis.memory.chunking.StepCapsuleLens`.
    """

    name = "lens"

    async def render(self, key: JobKey, payload: dict[str, Any]) -> str | None:
        """Produce the summary text for one payload, or None on failure."""
        raise NotImplementedError


class StepMemoryService:
    """Non-blocking background runtime for per-step summary jobs.

    Key design properties:
    1. Zero-blocking dispatch: callers submit and proceed immediately.
    2. Bounded retry: each job attempts at most ``1 + retry_limit`` times and
       then enters an explicit failed state — never an unbounded loop.
    3. Bounded flush: ``flush()`` waits up to the configured timeout, then
       cancels whatever is still in flight.
    4. Canonical keying with aliases: jobs are keyed by the DataEngine step id
       where available; tool_call_ids (and legacy ordinals) remain queryable
       through an alias map so message-level consumers need no migration.
    5. Bounded concurrency: at most ``max_concurrency`` attempts run at once.

    Subclasses implement :meth:`_attempt` (one summarization attempt for a
    payload) and may override :meth:`_on_status` to persist pending/failed
    status transitions.
    """

    def __init__(
        self,
        ctx: Any,
        *,
        lens: StepLens | None = None,
        max_concurrency: int = 1,
        retry_limit: int = 3,
        flush_timeout_s: float = 30.0,
    ):
        self.ctx = ctx
        self._lens = lens
        self._summaries: dict[JobKey, str] = {}
        self._failed: set[JobKey] = set()
        self._pending_tasks: dict[JobKey, asyncio.Task] = {}
        self._step_inputs: dict[JobKey, dict[str, Any]] = {}
        self._retry_counts: dict[JobKey, int] = {}
        self._retry_delays = (0.0, 0.5, 1.0, 2.0, 3.0)
        self._retry_limit = max(0, retry_limit)
        self._flush_timeout_s = max(0.0, flush_timeout_s)
        self._aliases: dict[JobKey, JobKey] = {}
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))

    # ------------------------------------------------------------------
    # Keying
    # ------------------------------------------------------------------

    def resolve_key(self, key: JobKey) -> JobKey:
        """Resolve an alias (tool_call_id / legacy ordinal) to its canonical key."""
        return self._aliases.get(key, key)

    def submit(
        self,
        key: JobKey,
        payload: dict[str, Any],
        aliases: tuple[JobKey, ...] = (),
    ) -> None:
        """Submit one summary job without blocking the caller.

        Args:
            key: Canonical job key (DataEngine step id where available).
            payload: Lens-specific input data; stored until the job resolves.
            aliases: Additional keys (e.g. tool_call_id) that resolve to ``key``.
        """
        for alias in aliases:
            if alias is not None and alias != key:
                self._aliases[alias] = key

        self._step_inputs[key] = payload
        self._failed.discard(key)
        self._on_status(key, "pending")

        task = asyncio.create_task(self._run_until_ready(key))
        self._pending_tasks[key] = task

    # ------------------------------------------------------------------
    # Scheduling core
    # ------------------------------------------------------------------

    async def _run_until_ready(self, key: JobKey) -> None:
        """Retry one job independently, bounded by the configured retry limit.

        Attempts at most ``1 + retry_limit`` times; on exhaustion the job
        enters an explicit failed state (surfaced via :meth:`has_failed` and
        the ``_on_status`` hook). Retry backoff sleeps happen outside the
        concurrency semaphore so a waiting job never starves its peers.
        """
        while key not in self._summaries:
            async with self._semaphore:
                succeeded = await self._attempt(key)
            if succeeded:
                return

            retry_count = self._retry_counts.get(key, 0) + 1
            self._retry_counts[key] = retry_count
            payload = self._step_inputs.get(key)
            display_step = payload.get("step_number") if payload else key

            if retry_count > self._retry_limit:
                self._failed.add(key)
                logger.warning(
                    f"StepMemoryService: Giving up on Step {display_step} after"
                    f" {retry_count} failed attempts (retry_limit={self._retry_limit})."
                )
                self._on_status(key, "failed")
                return

            delay = self._retry_delays[min(retry_count - 1, len(self._retry_delays) - 1)]
            logger.info(
                f"StepMemoryService: Retrying Step {display_step} "
                f"(attempt {retry_count + 1}) after {delay:.1f}s"
            )
            if delay:
                await asyncio.sleep(delay)

    async def _attempt(self, key: JobKey) -> bool:
        """Run one summarization attempt for ``key``; True when a summary landed.

        The default implementation delegates to the configured :class:`StepLens`;
        subclasses without a lens override this method directly (the Flash
        visual summarizer keeps its inlined attempt for compatibility).
        """
        if self._lens is None:
            raise NotImplementedError
        payload = self._step_inputs.get(key)
        if payload is None:
            return False
        try:
            summary = await self._lens.render(key, payload)
        except Exception as e:
            logger.warning(f"StepMemoryService: lens '{self._lens.name}' attempt failed: {e}")
            return False
        if not summary:
            return False
        self._summaries[key] = summary
        self._on_ready(key, summary)
        return True

    def _on_ready(self, key: JobKey, summary: str) -> None:
        """Hook invoked when a lens-produced summary lands; default is a no-op."""

    def _on_status(self, key: JobKey, status: str) -> None:
        """Hook invoked on pending/failed transitions; default is a no-op."""

    # ------------------------------------------------------------------
    # Status queries (alias-resolving)
    # ------------------------------------------------------------------

    def get_summary(self, key: JobKey, fallback_text: str | None = None) -> str | None:
        """Retrieve the summary for a job key (or alias), or fallback text."""
        return self._summaries.get(self.resolve_key(key), fallback_text)

    def has_summary(self, key: JobKey) -> bool:
        """Check whether a job's summary has completed."""
        return self.resolve_key(key) in self._summaries

    def has_job(self, key: JobKey) -> bool:
        """Check whether a job has been submitted for summarization."""
        return self.resolve_key(key) in self._step_inputs

    def has_failed(self, key: JobKey) -> bool:
        """Check whether a job permanently failed (retries exhausted)."""
        return self.resolve_key(key) in self._failed

    def is_pending(self, key: JobKey) -> bool:
        """Check whether a job still needs a summary."""
        return self.has_job(key) and not self.has_summary(key)

    def get_job_payload(self, key: JobKey) -> dict[str, Any] | None:
        """Return the stored payload for a job key (or alias), if any."""
        return self._step_inputs.get(self.resolve_key(key))

    def get_step_number(self, key: JobKey) -> int | None:
        """Return the human-facing step ordinal recorded for a job, if known."""
        payload = self.get_job_payload(key)
        if payload is None:
            return None
        step_number = payload.get("step_number")
        return step_number if isinstance(step_number, int) else None

    # ------------------------------------------------------------------
    # Draining
    # ------------------------------------------------------------------

    async def flush(self, timeout_seconds: float | None = None) -> None:
        """Wait for active jobs up to a bound, then cancel remaining retry loops."""
        if timeout_seconds is None:
            timeout_seconds = self._flush_timeout_s
        tasks = [t for t in self._pending_tasks.values() if not t.done()]
        if tasks:
            logger.info(f"StepMemoryService: Flushing {len(tasks)} pending summary tasks...")
            _, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
            if pending:
                logger.warning(
                    f"StepMemoryService: Cancelling {len(pending)} unfinished summary tasks "
                    f"after {timeout_seconds:.1f}s flush timeout."
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
