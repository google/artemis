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

"""SummarizerNode tests (M2: visual-transition lens dispatch).

Per the history redesign §5 the Pro SummarizerNode no longer runs its own LLM
call to produce a text capsule: it dispatches the step to the shared
step-memory service (``ctx.step_memory``) with the pre/post screenshot bytes
pulled from the DataEngine, and degrades gracefully when images are missing.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from artemis.agents.summarizer.summarizer import SummarizerNode
from artemis.context import ArtemisContext


class DummyState:
    def __init__(
        self,
        structured_decisions=None,
        operator_raw_thinking=None,
        operator_native_thinking=None,
        last_execution_result=None,
        current_step_id=None,
    ):
        self.structured_decisions = structured_decisions
        self.operator_raw_thinking = operator_raw_thinking
        self.operator_native_thinking = operator_native_thinking
        self.last_execution_result = last_execution_result
        self.current_step_id = current_step_id


@pytest.fixture
def mock_context(tmp_path):
    ctx = Mock(spec=ArtemisContext)
    ctx.device = None
    ctx.background_tasks = []
    ctx.step_memory = MagicMock()

    pre_path = tmp_path / "pre.jpg"
    post_path = tmp_path / "post.jpg"
    pre_path.write_bytes(b"PRE_IMAGE")
    post_path.write_bytes(b"POST_IMAGE")

    engine = Mock()
    engine.get_step_number.return_value = 7
    engine.get_step_record.return_value = SimpleNamespace(
        action_taken=[
            {
                "action": "tap",
                "coordinates": [540, 1440],
                "coordinate_space": "pixel",
                "target_text": "Search",
            }
        ]
    )
    engine.get_step_image_path.side_effect = lambda step_number, which="pre": (
        pre_path if which == "pre" else post_path
    )
    ctx.data_engine = engine
    return ctx


@pytest.mark.asyncio
async def test_summarizer_dispatches_visual_lens(mock_context):
    """The node dispatches to the shared service instead of calling an LLM."""
    state = DummyState(
        last_execution_result={"status": "success"},
        current_step_id="12345678-1234-5678-1234-567812345678",
    )

    node = SummarizerNode(mock_context)
    result = await node(state)

    assert result == {}
    mock_context.step_memory.dispatch.assert_called_once_with(
        step_number=7,
        action_name="tap",
        action_args={
            "coordinates": [500, 600],
            "coordinate_space": "normalized",
            "target_text": "Search",
        },
        pre_img_bytes=b"PRE_IMAGE",
        post_img_bytes=b"POST_IMAGE",
        exec_outcome="success",
        data_engine_step_id="12345678-1234-5678-1234-567812345678",
    )
    # No direct capsule write from the node: the lens owns the versioned
    # summary write when its background job completes.
    mock_context.data_engine.update_step_summary.assert_not_called()


@pytest.mark.asyncio
async def test_summarizer_degrades_gracefully_without_images(mock_context):
    """Missing DataEngine screenshots dispatch with None bytes, no exception."""
    mock_context.data_engine.get_step_image_path.side_effect = None
    mock_context.data_engine.get_step_image_path.return_value = None

    state = DummyState(
        last_execution_result={"status": "failed", "error": "tap missed"},
        current_step_id="12345678-1234-5678-1234-567812345678",
    )

    node = SummarizerNode(mock_context)
    await node(state)

    kwargs = mock_context.step_memory.dispatch.call_args.kwargs
    assert kwargs["pre_img_bytes"] is None
    assert kwargs["post_img_bytes"] is None
    assert kwargs["exec_outcome"] == "Error: tap missed"


@pytest.mark.asyncio
async def test_summarizer_falls_back_to_structured_decisions(mock_context):
    """Without a step record the action comes from structured_decisions, and
    extra actions ride along as additional_actions."""
    mock_context.data_engine.get_step_record.return_value = None

    state = DummyState(
        structured_decisions=(
            '[{"action": "swipe", "coordinates": [540, 1800, 540, 600],'
            ' "coordinate_space": "pixel"},'
            ' {"action": "tap", "coordinates": [540, 1200], "coordinate_space": "pixel"}]'
        ),
        last_execution_result={"status": "success"},
        current_step_id="12345678-1234-5678-1234-567812345678",
    )

    node = SummarizerNode(mock_context)
    await node(state)

    kwargs = mock_context.step_memory.dispatch.call_args.kwargs
    assert kwargs["action_name"] == "swipe"
    assert kwargs["action_args"]["coordinates"] == [500, 750, 500, 250]
    assert kwargs["action_args"]["additional_actions"] == [
        {"action": "tap", "coordinates": [500, 500], "coordinate_space": "normalized"}
    ]


@pytest.mark.asyncio
async def test_summarizer_noop_without_step_id(mock_context):
    state = DummyState(current_step_id=None)

    node = SummarizerNode(mock_context)
    result = await node(state)

    assert result == {}
    mock_context.step_memory.dispatch.assert_not_called()
