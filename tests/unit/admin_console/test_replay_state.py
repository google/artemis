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

"""Tests that ``ReplayManager.instantiate_state`` builds a valid ``State``.

``State`` uses ``extra="forbid"``, so any unknown field makes replay fail.
"""

import json
from pathlib import Path

import pytest

from apps.admin_console import replay_manager as rm_module
from apps.admin_console.replay_manager import ReplayManager
from artemis.graph.state import State

SESSION_ID = "11111111-2222-3333-4444-555555555555"
REAL_DATA_DIR = Path(rm_module.TRACES_PATH) / "replay" / "data"


def _make_manager(tmp_path: Path, monkeypatch) -> ReplayManager:
    manager = ReplayManager(workspace_root=tmp_path, original_db_path=tmp_path / "missing.db")
    manager.test_data_dir = tmp_path / "data"
    # Chunking reads the master DB; the fixture dir is already "chunked".
    monkeypatch.setattr(manager, "_ensure_session_chunked", lambda session_id: None)
    return manager


def _write_step_dir(tmp_path: Path) -> Path:
    session_dir = tmp_path / "data" / f"{SESSION_ID}_chunked"
    step_dir = session_dir / "step_01"
    step_dir.mkdir(parents=True)
    (session_dir / ".chunked").touch()
    (session_dir / "session.json").write_text(
        json.dumps({"initial_goal": "Open Settings and enable dark mode"}), encoding="utf-8"
    )
    (step_dir / "step.json").write_text(
        json.dumps(
            {
                "step_id": "step-uuid-1",
                "session_id": SESSION_ID,
                "step_number": 1,
                "timestamp": 1790638731.79,
                "action_taken": {"action": "manage_app", "app_name": "Settings"},
            }
        ),
        encoding="utf-8",
    )
    (step_dir / "pre_image_meta.json").write_text(
        json.dumps(
            {
                "timestamp": 1790638724.78,
                "ui_tree": [
                    {"index": "0", "text": "16:38", "package": "com.android.systemui"},
                    {"index": "1", "text": "Display", "package": "com.android.settings"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (step_dir / "pre.jpg").write_bytes(b"\xff\xd8\xff")
    return step_dir


def test_instantiate_state_builds_valid_state_from_step_dir(tmp_path, monkeypatch):
    step_dir = _write_step_dir(tmp_path)
    manager = _make_manager(tmp_path, monkeypatch)

    state = manager.instantiate_state(SESSION_ID, 1)

    assert isinstance(state, State)
    assert state.initial_goal == "Open Settings and enable dark mode"
    assert state.current_step_id == "step-uuid-1"
    assert state.latest_screenshot == str(step_dir / "pre.jpg")
    assert state.latest_ui_hierarchy[1]["package"] == "com.android.settings"
    assert json.loads(state.structured_decisions)["action"] == "manage_app"
    assert state.subagent_calls == []
    # Must round-trip through the strict schema.
    State.model_validate(state.model_dump())


def _real_step_dirs() -> list[Path]:
    if not REAL_DATA_DIR.is_dir():
        return []
    return sorted(REAL_DATA_DIR.glob("*_chunked/step_01"))[:5]


@pytest.mark.parametrize("step_dir", _real_step_dirs(), ids=lambda p: p.parent.name[:8])
def test_instantiate_state_on_recorded_step_dirs(step_dir, monkeypatch):
    """Exercise real chunked traces when present locally (traces/ is gitignored)."""
    manager = ReplayManager()
    manager.test_data_dir = step_dir.parent.parent
    monkeypatch.setattr(manager, "_ensure_session_chunked", lambda session_id: None)
    monkeypatch.setattr(manager, "load_session_goal", lambda *a, **k: "recorded goal")
    session_id = step_dir.parent.name.removesuffix("_chunked")

    state = manager.instantiate_state(session_id, 1)

    assert isinstance(state, State)
    assert state.initial_goal == "recorded goal"
    assert state.current_step_id
