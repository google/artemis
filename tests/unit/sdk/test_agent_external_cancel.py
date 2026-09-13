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

"""A cancel marker dropped by another process must cancel the Agent's task."""

import asyncio

import pytest

from artemis.runtime import cancel_requests
from artemis.sdk.agent import Agent


@pytest.mark.asyncio
async def test_external_cancel_marker_cancels_running_task(tmp_path, monkeypatch):
    monkeypatch.setattr(cancel_requests, "get_temp_dir", lambda _subfolder=None: tmp_path / "c")
    monkeypatch.setenv("ARTEMIS_CANCEL_POLL_SECONDS", "0.05")

    agent = Agent.__new__(Agent)
    agent._session_id = "sess-cancel"

    async def _work() -> None:
        await asyncio.sleep(30)

    agent._current_task = asyncio.create_task(_work())
    watcher = asyncio.create_task(agent._watch_external_cancel("demo"))
    await asyncio.sleep(0.15)
    assert not agent._current_task.done()

    cancel_requests.request_cancel(session_id="sess-cancel", reason="stop from UI")

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(agent._current_task, timeout=2)
    await asyncio.wait_for(watcher, timeout=2)
    assert not cancel_requests.is_cancel_requested(session_id="sess-cancel")
