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

"""``/api/sessions/{id}/checks``: backfill source for the Checker panel."""

import json

from apps.admin_console.services import media_service as media_service_module
from apps.admin_console.services.media_service import MediaService


def test_get_session_checks_reads_ledger_and_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(media_service_module, "TRACES_PATH", tmp_path)
    session_dir = tmp_path / "sess-1"
    session_dir.mkdir()
    records = [
        {
            "attempt_id": "abc#1",
            "checkpoint_id": "abc",
            "subgoal_text": "Create the alarm",
            "item_text": "alarm exists",
            "kind": "verify",
            "when": "on_complete",
            "status": "passed",
            "evidence": "seen",
            "ts": 1.0,
        },
        {"attempt_id": "final#1", "checkpoint_id": "final", "status": "passed", "ts": 2.0},
    ]
    (session_dir / "check_ledger.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\nnot json\n\n", encoding="utf-8"
    )
    (session_dir / "run_outcome.json").write_text(
        json.dumps({"task_status": "completed", "tests": {"passed": 2, "failed": 0}}),
        encoding="utf-8",
    )

    result = MediaService.get_session_checks("sess-1")

    assert [r["attempt_id"] for r in result["records"]] == ["abc#1", "final#1"]
    assert result["records"][0]["subgoal_text"] == "Create the alarm"
    assert result["run_outcome"]["task_status"] == "completed"


def test_get_session_checks_without_material(tmp_path, monkeypatch):
    monkeypatch.setattr(media_service_module, "TRACES_PATH", tmp_path)
    (tmp_path / "sess-2").mkdir()

    assert MediaService.get_session_checks("sess-2") == {"records": [], "run_outcome": None}
    assert MediaService.get_session_checks("missing") == {"records": [], "run_outcome": None}


def test_checks_route_is_registered():
    from apps.admin_console.routers import media

    paths = {getattr(r, "path", None) for r in media.router.routes}
    assert "/api/sessions/{session_id}/checks" in paths
