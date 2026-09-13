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

"""Perception side of the guidance waiver: a non-empty user instruction records
the plan's current check lines in ``ctx.guidance_unprotected_checks`` on the run
context (which outlives the per-turn State rebuild); stop-only or empty
payloads do not, and an ordinary turn never touches the set."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.graph.perception import perception_node
from artemis.graph.state import State


async def _run_perception(ctx, payload):
    with (
        patch("artemis.graph.perception.is_ocr_configured", return_value=False),
        patch("artemis.graph.perception.UnifiedMobileController") as controller_cls,
        patch("artemis.graph.perception._check_injected_instruction_file", return_value=payload),
    ):
        device_data = MagicMock()
        device_data.width = 1080
        device_data.height = 2400
        device_data.base64 = "ZHVtbXk="
        device_data.elements = []
        controller = MagicMock()
        controller.get_screen_data = AsyncMock(return_value=device_data)
        controller_cls.return_value = controller

        state = MagicMock(spec=State)
        state.structured_decisions = []
        return await perception_node(state, ctx)


PLAN = "- [/] Set the alarm\n  - verify: the alarm list shows 7:30 AM\n- [ ] Next milestone\n"


def _ctx(tmp_path):
    from artemis.utils.notes import get_note_file_path

    ctx = MagicMock()
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = tmp_path
    ctx.data_engine.current_step_id = "step_1"
    ctx.device = MagicMock()
    ctx.guidance_unprotected_checks = set()
    plan_path = get_note_file_path(tmp_path, "task_plan")
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(PLAN, encoding="utf-8")
    return ctx


@pytest.mark.asyncio
async def test_instruction_waives_the_current_check_lines_and_still_reaches_state(tmp_path):
    ctx = _ctx(tmp_path)
    update = await _run_perception(ctx, {"instruction": "Skip the toast check"})
    assert ctx.guidance_unprotected_checks == {("verify", "the alarm list shows 7:30 AM")}
    assert update["injected_instruction"] == "Skip the toast check"
    assert update["user_stop_requested"] is False


@pytest.mark.asyncio
async def test_stop_only_or_blank_payload_does_not_arm(tmp_path):
    ctx = _ctx(tmp_path)
    update = await _run_perception(ctx, {"release_loop": True})
    assert update["user_stop_requested"] is True
    assert ctx.guidance_unprotected_checks == set()

    update = await _run_perception(ctx, {"instruction": "   ", "release_loop": True})
    assert update["user_stop_requested"] is True
    assert ctx.guidance_unprotected_checks == set()


@pytest.mark.asyncio
async def test_ordinary_turn_leaves_the_waiver_as_it_was(tmp_path):
    """The per-turn reset applies to State (``injected_instruction`` goes back
    to None); the waiver on the context lives for the rest of the run."""
    ctx = _ctx(tmp_path)
    ctx.guidance_unprotected_checks = {("verify", "earlier line")}
    update = await _run_perception(ctx, None)
    assert update["injected_instruction"] is None
    assert ctx.guidance_unprotected_checks == {("verify", "earlier line")}
