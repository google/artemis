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

"""Read-only access to live and stored session history.

DataEngine implements HistoryReader for active sessions. OfflineHistoryReader
opens an existing database and uses the same step renderer for trace inspection.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any, Protocol, runtime_checkable

from artemis.data_engine.engine import build_image_describer, friendly_step
from artemis.data_engine.models import HistoryChunkRecord, StepRecord
from artemis.data_engine.storage import StorageManager
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["HistoryReader", "OfflineHistoryReader", "friendly_step"]


@runtime_checkable
class HistoryReader(Protocol):
    """Everything the history tools need from a session's stored history."""

    session_start_time: float | None
    base_dir: Path
    storage: StorageManager

    def get_agent_friendly_steps(self) -> list[dict[str, Any]]: ...

    def get_agent_friendly_steps_in_range(
        self, start_step: int, end_step: int | None = None
    ) -> list[dict[str, Any]]: ...

    def get_agent_friendly_step(self, step_number: int) -> dict[str, Any] | None: ...

    def get_step_record(self, step_number: int) -> StepRecord | None: ...

    def get_step_image_path(self, step_number: int, which: str = "pre") -> Path | None: ...

    def get_image_path(self, image_name: str) -> Path: ...

    def get_history_chunks(self) -> list[HistoryChunkRecord]: ...


class OfflineHistoryReader:
    """Read-only history of one session straight from the traces database.

    ``traces_dir`` is the traces root (``data_engine.db`` and ``images/`` live
    there); ``base_dir`` resolves to the session's own directory, where its
    notes are stored — the same layout the live ``DataEngine`` writes.
    """

    def __init__(self, db_path: str | Path, traces_dir: str | Path, session_id: Any):
        self.global_base_dir = Path(traces_dir)
        self.session_id = str(session_id)
        self.storage = StorageManager(db_path, traces_dir, read_only=True)
        self.base_dir = self.global_base_dir / self.session_id
        self.session_start_time: float | None = None
        try:
            session = self.storage.get_session(self.session_id)
            self.session_start_time = session.start_time if session else None
        except (sqlite3.Error, ValueError) as e:
            logger.debug(f"Session clock unavailable for {self.session_id}: {e}")

    # --- Images ----------------------------------------------------------------------

    def get_image_path(self, image_name: str) -> Path:
        return self.global_base_dir / "images" / f"{image_name}.jpg"

    def get_step_image_path(self, step_number: int, which: str = "pre") -> Path | None:
        step = self.get_step_record(step_number)
        if step is None:
            return None
        image_name = step.pre_image_name if which == "pre" else step.post_image_name
        if not image_name:
            return None
        path = self.get_image_path(image_name)
        return path if path.exists() else None

    # --- Steps -----------------------------------------------------------------------

    def _steps(self) -> list[StepRecord]:
        return self.storage.get_steps(self.session_id) or []

    def _friendly(self, step: StepRecord, describer) -> dict[str, Any]:
        traces = self.storage.get_traces_for_step(step.step_id)
        return friendly_step(step, traces, describer, session_start_time=self.session_start_time)

    def get_agent_friendly_steps(self) -> list[dict[str, Any]]:
        steps = self._steps()
        describer = build_image_describer(steps)
        return [self._friendly(step, describer) for step in steps]

    def get_agent_friendly_steps_in_range(
        self, start_step: int, end_step: int | None = None
    ) -> list[dict[str, Any]]:
        end_step = start_step if end_step is None else end_step
        if start_step > end_step:
            start_step, end_step = end_step, start_step
        steps = self._steps()
        describer = build_image_describer(steps)
        return [
            self._friendly(step, describer)
            for step in steps
            if start_step <= step.step_number <= end_step
        ]

    def get_agent_friendly_step(self, step_number: int) -> dict[str, Any] | None:
        rows = self.get_agent_friendly_steps_in_range(step_number, step_number)
        return rows[0] if rows else None

    def get_step_record(self, step_number: int) -> StepRecord | None:
        for step in self._steps():
            if step.step_number == step_number:
                return step
        return None

    # --- Chunks ----------------------------------------------------------------------

    def get_history_chunks(self) -> list[HistoryChunkRecord]:
        try:
            return self.storage.get_history_chunks(self.session_id)
        except (sqlite3.Error, ValueError) as e:
            logger.debug(f"History chunks unavailable for {self.session_id}: {e}")
            return []
