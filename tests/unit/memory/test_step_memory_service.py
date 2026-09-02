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

"""Unit tests for the shared StepMemoryService scheduling runtime (M1)."""

import asyncio
from unittest.mock import Mock

import pytest

from artemis.memory.step_memory import StepMemoryService


class _ScriptedService(StepMemoryService):
    """StepMemoryService with a scriptable attempt function and status log."""

    def __init__(self, attempt_fn, **kwargs):
        super().__init__(Mock(), **kwargs)
        self._attempt_fn = attempt_fn
        self.status_events: list[tuple] = []

    async def _attempt(self, key):
        return await self._attempt_fn(self, key)

    def _on_status(self, key, status):
        self.status_events.append((key, status))


@pytest.mark.asyncio
async def test_zero_blocking_submit_and_flush():
    """submit() returns immediately; flush() drains the background job."""
    release = asyncio.Event()

    async def attempt(svc, key):
        await release.wait()
        svc._summaries[key] = f"summary-for-{key}"
        return True

    service = _ScriptedService(attempt)
    service.submit("step-1", {"step_number": 1})

    # Zero-blocking: the job cannot have completed yet (its gate is closed).
    assert service.is_pending("step-1")
    assert not service.has_summary("step-1")

    release.set()
    await service.flush()

    assert service.get_summary("step-1") == "summary-for-step-1"
    assert not service.is_pending("step-1")


@pytest.mark.asyncio
async def test_bounded_retry_enters_failed_state():
    """A permanently failing job stops after 1 + retry_limit attempts."""
    attempts = 0

    async def attempt(svc, key):
        nonlocal attempts
        attempts += 1
        return False

    service = _ScriptedService(attempt, retry_limit=2)
    service._retry_delays = (0.0,)
    service.submit("step-x", {"step_number": 7})
    await service.flush()

    assert attempts == 3
    assert service.has_failed("step-x")
    assert not service.has_summary("step-x")
    # The failed job still reads as pending (lossless semantics downstream).
    assert service.is_pending("step-x")
    assert service.status_events == [("step-x", "pending"), ("step-x", "failed")]


@pytest.mark.asyncio
async def test_step_id_keying_with_tool_call_alias():
    """Jobs keyed by step id remain queryable through tool_call_id aliases."""

    async def attempt(svc, key):
        svc._summaries[key] = "canonical summary"
        return True

    service = _ScriptedService(attempt)
    service.submit("uuid-123", {"step_number": 4}, aliases=("tc-abc",))
    await service.flush()

    assert service.resolve_key("tc-abc") == "uuid-123"
    assert service.has_job("tc-abc")
    assert service.has_summary("tc-abc")
    assert service.get_summary("tc-abc") == "canonical summary"
    assert service.get_summary("uuid-123") == "canonical summary"
    assert service.get_job_payload("tc-abc") == {"step_number": 4}
    assert not service.has_failed("tc-abc")


@pytest.mark.asyncio
async def test_max_concurrency_bounds_parallel_attempts():
    """With max_concurrency=1, two jobs never run their attempts concurrently."""
    active = 0
    max_active = 0

    async def attempt(svc, key):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        svc._summaries[key] = "done"
        return True

    service = _ScriptedService(attempt, max_concurrency=1)
    service.submit("a", {"step_number": 1})
    service.submit("b", {"step_number": 2})
    await service.flush()

    assert service.has_summary("a") and service.has_summary("b")
    assert max_active == 1


@pytest.mark.asyncio
async def test_flush_timeout_cancels_stragglers():
    """flush() cancels jobs that outlive the timeout without failing them."""

    async def attempt(svc, key):
        await asyncio.sleep(30)
        return True

    service = _ScriptedService(attempt)
    service.submit("slow", {"step_number": 1})
    await service.flush(timeout_seconds=0.05)

    assert service.is_pending("slow")
    assert not service.has_failed("slow")
    assert service._pending_tasks["slow"].done()


@pytest.mark.asyncio
async def test_flush_uses_configured_default_timeout():
    """flush() with no argument honors the constructor's flush_timeout_s."""

    async def attempt(svc, key):
        await asyncio.sleep(30)
        return True

    service = _ScriptedService(attempt, flush_timeout_s=0.05)
    service.submit("slow", {"step_number": 1})
    await asyncio.wait_for(service.flush(), timeout=5)

    assert service._pending_tasks["slow"].done()


@pytest.mark.asyncio
async def test_resubmit_clears_failed_state():
    """Re-submitting a failed job gives it a fresh retry budget."""
    behaviors = {"fail": True}

    async def attempt(svc, key):
        if behaviors["fail"]:
            return False
        svc._summaries[key] = "second time lucky"
        return True

    service = _ScriptedService(attempt, retry_limit=0)
    service._retry_delays = (0.0,)
    service.submit("job", {"step_number": 1})
    await service.flush()
    assert service.has_failed("job")

    behaviors["fail"] = False
    service._retry_counts.clear()
    service.submit("job", {"step_number": 1})
    await service.flush()

    assert not service.has_failed("job")
    assert service.get_summary("job") == "second time lucky"
