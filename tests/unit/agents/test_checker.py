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

from unittest.mock import AsyncMock, MagicMock, patch

from artemis.agents.checker.checker import CheckerResult, run_async_check
from artemis.context import ArtemisContext
import pytest


@pytest.mark.asyncio
async def test_run_async_check(tmp_path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    # Put a dummy task plan file
    (notes_dir / "task_plan.md").write_text("Plan")

    # Put a dummy verification_chat file
    import hashlib

    subgoal_text = "Open WhatsApp"
    s_hash = hashlib.md5(subgoal_text.encode("utf-8")).hexdigest()
    (notes_dir / f"verification_chat_{s_hash}.json").write_text("[]")

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = str(tmp_path)
    mock_ctx.data_engine.get_agent_friendly_steps.return_value = []
    mock_ctx.execution_setup = None

    # Mock controller for screen data
    mock_controller = MagicMock()
    mock_controller.take_screenshot = AsyncMock(return_value="dummy_screenshot_base64")

    from artemis.controllers.device_controller import ScreenDataResponse

    mock_screen_data = ScreenDataResponse(
        base64="dummy_screenshot_base64",
        elements=[],
        width=1080,
        height=2400,
        platform="android",
    )
    mock_controller.get_screen_data = AsyncMock(return_value=mock_screen_data)

    # Mock LLM response with with_structured_output
    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.content = "Task completed successfully."
    mock_response.tool_calls = []

    async def mock_astream(*args, **kwargs):
        yield mock_response

    mock_llm.astream.side_effect = mock_astream
    mock_llm.bind_tools.return_value = mock_llm

    mock_structured_llm = MagicMock()
    mock_structured_llm.ainvoke = AsyncMock(
        return_value=CheckerResult(success=True, reason="Task completed successfully.")
    )
    mock_llm.with_structured_output.return_value = mock_structured_llm

    with (
        patch(
            "artemis.agents.checker.checker.UnifiedMobileController",
            return_value=mock_controller,
        ),
        patch("artemis.agents.checker.checker.get_llm", return_value=mock_llm),
        patch(
            "artemis.utils.task_tree.build_plan_and_history",
            return_value="Task tree",
        ),
    ):
        result = await run_async_check(
            mock_ctx,
            subgoal_text=subgoal_text,
            subgoal_hash=s_hash,
            raw_perception_data={"screenshot_b64": "dummy", "width": 1080, "height": 2400},
            latest_ui_hierarchy=[],
        )

        assert result["status"] == "success"
        assert result["reason"] == "Task completed successfully."
        assert mock_llm.astream.called
        assert mock_llm.with_structured_output.called
