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

from langchain_core.tools import BaseTool
from artemis.agents.operator.operator import OperatorNode
import pytest


@pytest.mark.asyncio
async def test_operator_tools(artemis_context, mock_state):
    """Test all tools exposed by the OperatorNode."""
    node = OperatorNode(ctx=artemis_context)

    click_tool = node._get_click_tool()
    assert isinstance(click_tool, BaseTool)
    res = click_tool.invoke({"target": 1, "times": 1, "delay_ms": 100})
    assert res == "Action Recorded"

    input_text_tool = node._get_input_text_tool()
    assert isinstance(input_text_tool, BaseTool)
    res = input_text_tool.invoke({"text": "hello", "target": 1, "clear_exist": True})
    assert res == "Action Recorded"

    swipe_tool = node._get_swipe_tool()
    assert isinstance(swipe_tool, BaseTool)
    res = swipe_tool.invoke({"gesture": "up"})
    assert res == "Action Recorded"

    press_key_tool = node._get_press_key_tool()
    assert isinstance(press_key_tool, BaseTool)
    res = press_key_tool.invoke({"key": "ENTER"})
    assert res == "Action Recorded"

    manage_app_tool = node._get_manage_app_tool()
    assert isinstance(manage_app_tool, BaseTool)
    res = manage_app_tool.invoke({"action": "launch", "app_name": "Settings"})
    assert res == "Action Recorded"

    wait_for_delay_tool = node._get_wait_for_delay_tool()
    assert isinstance(wait_for_delay_tool, BaseTool)
    res = wait_for_delay_tool.invoke({"time_in_ms": 1000})
    assert res == "Action Recorded"

    long_press_tool = node._get_long_press_tool()
    assert isinstance(long_press_tool, BaseTool)
    res = long_press_tool.invoke({"target": 1, "duration": 1000})
    assert res == "Action Recorded"

    wait_for_text_tool = node._get_wait_for_text_tool()
    assert isinstance(wait_for_text_tool, BaseTool)
    res = wait_for_text_tool.invoke({"text": "Success", "state": "visible", "timeout_ms": 5000})
    assert res == "Action Recorded"

    reply_tool = node._get_reply_to_checker_tool()
    assert isinstance(reply_tool, BaseTool)
    res = reply_tool.invoke({"reasoning": "This is a test reply."})
    assert isinstance(res, str)
