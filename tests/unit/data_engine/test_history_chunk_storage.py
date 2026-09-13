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

"""History-chunk storage (M3 §6.1): append-only versioned rows keyed by step range."""

from uuid import uuid4

import pytest

from artemis.data_engine.models import HistoryChunkRecord
from artemis.data_engine.storage import StorageManager

SESSION = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def storage(tmp_path):
    return StorageManager(tmp_path / "test.db", tmp_path / "traces")


def _chunk(start: int, end: int, version: int = 1, **overrides) -> HistoryChunkRecord:
    fields = {
        "chunk_id": uuid4(),
        "session_id": SESSION,
        "start_step_id": f"sid-{start}",
        "end_step_id": f"sid-{end}",
        "start_step_number": start,
        "end_step_number": end,
        "source_step_ids": [f"sid-{n}" for n in range(start, end + 1)],
        "subgoal_hash": "hash-a",
        "version": version,
        "status": "pending",
        "band1": {},
        "band2": None,
        "band3": f"- Step {start} (T+00:10): Tapped 'x' at [1, 2] -> executed",
        "rendered_text": "[Chunk 1 | ...]",
    }
    fields.update(overrides)
    return HistoryChunkRecord(**fields)


def test_history_chunk_roundtrip(storage):
    storage.create_history_chunk(_chunk(1, 4))
    chunks = storage.get_history_chunks(SESSION)
    assert len(chunks) == 1
    rec = chunks[0]
    assert (rec.start_step_number, rec.end_step_number) == (1, 4)
    assert rec.source_step_ids == [f"sid-{n}" for n in range(1, 5)]
    assert rec.subgoal_hash == "hash-a"
    assert rec.status == "pending"
    assert "T+00:10" in rec.band3


def test_history_chunk_versions_are_append_only_latest_wins(storage):
    storage.create_history_chunk(_chunk(1, 4, version=1, status="pending"))
    storage.create_history_chunk(
        _chunk(
            1,
            4,
            version=2,
            status="ready",
            band1={"doing": "d", "verified_facts": ["f1"]},
            band2="  - Steps 1–4: did things",
        )
    )

    latest = storage.get_history_chunks(SESSION)
    assert len(latest) == 1
    assert latest[0].version == 2
    assert latest[0].status == "ready"
    assert latest[0].band1["verified_facts"] == ["f1"]

    trail = storage.get_history_chunks(SESSION, all_versions=True)
    assert [r.version for r in trail] == [1, 2]


def test_history_chunks_ordered_by_step_range(storage):
    storage.create_history_chunk(_chunk(9, 12))
    storage.create_history_chunk(_chunk(1, 4))
    storage.create_history_chunk(_chunk(5, 8))
    latest = storage.get_history_chunks(SESSION)
    assert [(r.start_step_number, r.end_step_number) for r in latest] == [
        (1, 4),
        (5, 8),
        (9, 12),
    ]


def test_history_chunks_are_session_scoped(storage):
    storage.create_history_chunk(_chunk(1, 4))
    other = "22222222-2222-2222-2222-222222222222"
    storage.create_history_chunk(_chunk(1, 4, session_id=other))
    assert len(storage.get_history_chunks(SESSION)) == 1
    assert len(storage.get_history_chunks(other)) == 1
