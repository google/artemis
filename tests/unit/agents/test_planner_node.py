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

from unittest.mock import Mock, patch

from artemis.agents.planner.planner import PlannerNode
from artemis.context import DevicePlatform, ArtemisContext
import pytest


class DummyState:
    def __init__(
        self,
        initial_goal,
        needs_replan=False,
        operator_raw_data=None,
        operator_replan_reason=None,
    ):
        self.initial_goal = initial_goal
        self.needs_replan = needs_replan
        self.operator_raw_data = operator_raw_data
        self.operator_replan_reason = operator_replan_reason

    async def asanitize_update(self, ctx, update, agent):
        return update


@pytest.fixture
def mock_context():
    ctx = Mock(spec=ArtemisContext)
    ctx.device = Mock()
    ctx.device.mobile_platform = DevicePlatform.ANDROID
    ctx.adb_client = Mock()
    ctx.ui_adb_client = Mock()
    mock_screen_data = Mock()
    mock_screen_data.base64 = "YmFzZTY0"
    mock_screen_data.elements = []
    mock_screen_data.width = 1080
    mock_screen_data.height = 2400
    ctx.ui_adb_client.get_screen_data.return_value = mock_screen_data
    ctx.data_engine = None

    return ctx


@pytest.mark.asyncio
async def test_planner_initial_plan(mock_context):
    state = DummyState(
        initial_goal="Test Goal",
        operator_raw_data={"screenshot_b64": "dummy_base64_string"},
    )
    node = PlannerNode(mock_context)

    mock_llm = Mock()
    mock_response = Mock()
    mock_response.content = "- [ ] Subgoal 1"
    mock_response.tool_calls = []

    async def mock_astream(*args, **kwargs):
        yield mock_response

    mock_llm.astream.side_effect = mock_astream
    mock_llm.bind_tools = Mock(return_value=mock_llm)

    with patch("artemis.agents.planner.planner.get_llm", return_value=mock_llm):
        await node(state)

        assert mock_llm.astream.called
        args, kwargs = mock_llm.astream.call_args
        messages = args[0]

        system_msg = messages[0].content
        assert "## Role" in system_msg

        human_msg_content = messages[1].content
        assert isinstance(human_msg_content, list)
        assert human_msg_content[0]["type"] == "text"
        assert "Goal: Test Goal" in human_msg_content[0]["text"]
