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

from artemis.context import ArtemisContext
from artemis.graph.state import State
from artemis.tools.object_detection_tool import get_operator_object_detector_tool
import pytest


@pytest.mark.asyncio
async def test_object_detection_trace():
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/path/to/screenshot.jpg"

    # We need to mock the decorator in object_detection_tool.py if we want to avoid it calling real get_llm
    # Or we can just mock get_llm.
    # In the tool definition: @trace(type="tool", ctx=ctx)
    # This calls record_trace immediately on entry/exit.

    mock_tool = get_operator_object_detector_tool(mock_ctx)

    # Mock the internal function called by the tool
    with patch(
        "artemis.tools.object_detection_tool._run_object_detection",
        AsyncMock(return_value="{}"),
    ) as mock_run:
        # We need to handle the fact that ainvoke might fail if LangChain internals are not fully mocked.
        # But let's try it.

        await mock_tool.ainvoke(
            {
                "type": "tool_call",
                "name": "object_detection",
                "id": "call_123",
                "args": {"queries": ["test_label"], "state": mock_state},
            }
        )

        # Verify _run_object_detection was called
        mock_run.assert_called_once()

        # Verify record_trace was called for the span
        assert mock_ctx.data_engine.record_trace.call_count >= 1

        calls = mock_ctx.data_engine.record_trace.call_args_list
        span_called = False
        for call in calls:
            kwargs = call.kwargs
            if kwargs.get("type") == "span" and kwargs.get("name") == "run_object_detection":
                span_called = True
                break

        assert span_called, "TraceSpan 'run_object_detection' was not called"
