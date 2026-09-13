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

import asyncio
import json
import time

import pytest

from artemis.runtime import cancel_requests as cr


@pytest.fixture(autouse=True)
def isolated_dir(tmp_path, monkeypatch):
    directory = tmp_path / "cancel-requests"
    monkeypatch.setattr(cr, "get_temp_dir", lambda _subfolder=None: directory)
    return directory


def test_session_marker_roundtrip(isolated_dir):
    assert not cr.is_cancel_requested(session_id="s-1")

    written = cr.request_cancel(session_id="s-1", reason="test")

    assert len(written) == 1
    assert written[0].parent == isolated_dir
    assert cr.is_cancel_requested(session_id="s-1")

    cr.clear_cancel_request(session_id="s-1")

    assert not cr.is_cancel_requested(session_id="s-1")


def test_pid_marker_ignores_recycled_pid():
    cr.request_cancel(pid=4242, process_created_at=1000.0)

    assert cr.is_cancel_requested(pid=4242, process_created_at=1000.4)
    assert not cr.is_cancel_requested(pid=4242, process_created_at=2000.0)

    # A marker without a creation time applies to whoever holds the pid.
    cr.request_cancel(pid=4343)
    assert cr.is_cancel_requested(pid=4343, process_created_at=5.0)


def test_expired_markers_are_ignored_and_purged():
    path = cr.request_cancel(session_id="old")[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["requested_at"] = time.time() - cr.MARKER_TTL_SECONDS - 5
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert not cr.is_cancel_requested(session_id="old")
    assert cr.purge_expired() == 1
    assert not path.exists()


def test_unreadable_pid_marker_is_not_trusted(isolated_dir):
    isolated_dir.mkdir(parents=True, exist_ok=True)
    (isolated_dir / "pid-77.cancel").write_text("{not json", encoding="utf-8")

    assert not cr.is_cancel_requested(pid=77, process_created_at=1.0)


@pytest.mark.asyncio
async def test_watch_delivers_once_and_clears_marker():
    delivered: list[int] = []
    watcher = asyncio.create_task(
        cr.watch_for_cancel_request(
            lambda: delivered.append(1),
            session_id="w-1",
            pid=0,
            poll_seconds=0.05,
        )
    )
    await asyncio.sleep(0.12)
    assert not delivered
    assert not watcher.done()

    cr.request_cancel(session_id="w-1")

    assert await asyncio.wait_for(watcher, timeout=2) is True
    assert delivered == [1]
    assert not cr.is_cancel_requested(session_id="w-1")
