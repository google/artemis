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
import pytest

from artemis.mcp.action_specs import OPERATOR_SHELL_ORDER, operator_shell_tool


@pytest.mark.asyncio
async def test_operator_shell_tools():
    """Every manifest-generated Operator shell is a declaration-only LangChain tool."""
    sample_args = {
        "click": {"target": 1, "times": 1, "delay_ms": 100},
        "input_text": {"text": "hello", "target": 1, "clear_exist": True},
        "swipe": {"gesture": "up"},
        "press_key": {"key": "ENTER"},
        "manage_app": {"action": "launch", "app_name": "Settings"},
        "wait_for_delay": {"time_in_ms": 1000},
        "long_press": {"target": 1, "duration": 1000},
    }
    assert set(sample_args) == set(OPERATOR_SHELL_ORDER)

    for name in OPERATOR_SHELL_ORDER:
        tool = operator_shell_tool(name)
        assert isinstance(tool, BaseTool)
        assert tool.name == name
        assert tool.invoke(sample_args[name]) == "Action Recorded"
