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

"""Unit tests for VisualStepSummarizer and Context Compressor in Flash profile."""

import asyncio
from unittest.mock import AsyncMock, Mock
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from artemis.agents.flash.context_compressor import compress_flash_messages
from artemis.agents.flash.summarizer import VisualStepSummarizer
from artemis.context import ArtemisContext
from artemis.sdk.builders import Builders


@pytest.fixture
def mock_context():
    ctx = Mock(spec=ArtemisContext)
    ctx.data_engine = None
    return ctx


@pytest.mark.asyncio
async def test_summarizer_dispatch_and_caching(mock_context):
    """Verify non-blocking dispatch and summary generation."""
    summarizer = VisualStepSummarizer(mock_context)

    mock_llm_response = AIMessage(
        content="[Step 1 State: Tapped 'Login' button. Home view opened.]"
    )
    summarizer._llm = Mock()
    summarizer._llm.ainvoke = AsyncMock(return_value=mock_llm_response)

    summarizer.dispatch(
        step_number=1,
        action_name="click",
        action_args={"target": [500, 600]},
        pre_img_bytes=b"fake_pre",
        post_img_bytes=b"fake_post",
        exec_outcome="Clicked successfully",
    )

    # Await background task flush
    await summarizer.flush()

    assert summarizer.has_summary(1)
    summary = summarizer.get_summary(1)
    assert "[Step 1 State: Tapped 'Login' button. Home view opened.]" in summary

    # Verify memory optimization: image buffers are released
    assert summarizer._step_inputs[1]["pre_img_bytes"] is None
    assert summarizer._step_inputs[1]["post_img_bytes"] is None


@pytest.mark.asyncio
async def test_summarizer_retrigger_stalled_steps(mock_context):
    """Verify that earlier stalled steps are re-triggered when subsequent steps arrive."""
    summarizer = VisualStepSummarizer(mock_context)
    attempt_count = 0

    async def mock_ainvoke(messages):
        nonlocal attempt_count
        attempt_count += 1
        # Fail the first attempt (Step 1 first call)
        if attempt_count == 1:
            raise asyncio.CancelledError("Simulated timeout")
        # Check messages for step number
        msg_str = str(messages)
        if "Step 1" in msg_str:
            return AIMessage(content="[Step 1 State: Recovered Step 1 summary]")
        return AIMessage(content="[Step 2 State: Step 2 summary]")

    summarizer._llm = Mock()
    summarizer._llm.ainvoke = AsyncMock(side_effect=mock_ainvoke)

    # Dispatch step 1 (fails on first attempt)
    summarizer.dispatch(
        step_number=1,
        action_name="click",
        action_args={"target": [100, 100]},
        pre_img_bytes=b"pre1",
        post_img_bytes=b"post1",
        exec_outcome="Outcome 1",
    )
    await summarizer.flush()
    assert not summarizer.has_summary(1)

    # Dispatch step 2 (should detect stalled Step 1 and re-trigger)
    summarizer.dispatch(
        step_number=2,
        action_name="swipe",
        action_args={"action": "up"},
        pre_img_bytes=b"pre2",
        post_img_bytes=b"post2",
        exec_outcome="Outcome 2",
    )
    await summarizer.flush()

    assert summarizer.has_summary(1)
    assert summarizer.has_summary(2)
    assert "Recovered Step 1" in summarizer.get_summary(1)


def test_context_compressor_with_ready_summaries(mock_context):
    """Verify that ready summaries replace past image blocks and prune outdated XML."""
    summarizer = VisualStepSummarizer(mock_context)
    summarizer._summaries[1] = "[Step 1 State: Tapped Search bar. Keyboard appeared.]"
    summarizer._summaries[2] = "[Step 2 State: Typed 'flights'. Suggestions displayed.]"

    messages = [
        HumanMessage(
            content=[
                {"type": "text", "text": "Task: Search flights"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,IMG_1"}},
                {"type": "text", "text": "--- UI Element List ---\nOld Tree 1"},
            ]
        ),
        ToolMessage(
            tool_call_id="tc1",
            name="click",
            content=[
                {"type": "text", "text": "Action 'click' completed."},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,IMG_2"}},
                {"type": "text", "text": "--- UI Element List ---\nOld Tree 2"},
            ],
        ),
        ToolMessage(
            tool_call_id="tc2",
            name="input_text",
            content=[
                {"type": "text", "text": "Action 'input_text' completed."},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,IMG_3"}},
                {"type": "text", "text": "--- UI Element List ---\nLive Latest Tree 3"},
            ],
        ),
    ]

    compress_flash_messages(messages, summarizer=summarizer, prune_history_xml=True)

    # Initial HumanMessage: Historical initial image pruned, task text preserved
    assert len(messages[0].content) == 1
    assert messages[0].content[0]["text"] == "Task: Search flights"

    # Step 1 (ToolMessage 1): Image replaced with Step 1 summary, old XML pruned
    assert len(messages[1].content) == 2
    assert messages[1].content[0]["text"] == "Action 'click' completed."
    assert messages[1].content[1]["text"] == "[Step 1 State: Tapped Search bar. Keyboard appeared.]"

    # Step 2 (ToolMessage 2 - Latest live screen): Image and latest live XML MUST be preserved
    assert len(messages[2].content) == 3
    assert messages[2].content[0]["text"] == "Action 'input_text' completed."
    assert messages[2].content[1]["type"] == "image_url"
    assert "Live Latest Tree 3" in messages[2].content[2]["text"]


def test_context_compressor_graceful_fallback(mock_context):
    """Verify that when a summary is not ready yet, it cleanly falls back to standard pruned state."""
    summarizer = VisualStepSummarizer(mock_context)
    # Step 1 has no summary in cache

    messages = [
        HumanMessage(
            content=[
                {"type": "text", "text": "Task: Search"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,IMG_1"}},
            ]
        ),
        ToolMessage(
            tool_call_id="tc1",
            name="click",
            content=[
                {"type": "text", "text": "Action 'click' completed."},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,IMG_2"}},
            ],
        ),
    ]

    compress_flash_messages(messages, summarizer=summarizer, prune_history_xml=True)

    # Turn 1 should simply have image omitted (standard fallback)
    assert len(messages[0].content) == 1
    assert messages[0].content[0]["text"] == "Task: Search"

    # Turn 2 (Latest) preserves image
    assert len(messages[1].content) == 2
    assert messages[1].content[1]["type"] == "image_url"


def test_flash_config_and_builder():
    """Verify Flash profile configuration model and SDK builder fluent API."""
    cfg = Builders.AgentConfig.with_flash_config(
        max_turns=25,
        explorer_mode="flash",
        step_summarizer=True,
        step_summarizer_model="gemini-3.5-flash-lite",
        prune_history_xml=True,
    ).build()

    assert cfg.flash.max_turns == 25
    assert cfg.flash.explorer_mode == "flash"
    assert cfg.flash.step_summarizer.enabled is True
    assert cfg.flash.step_summarizer.model == "gemini-3.5-flash-lite"
    assert cfg.flash.step_summarizer.prune_history_xml is True


@pytest.mark.asyncio
async def test_data_engine_integration_with_step_summarizer(mock_context):
    """Verify that generated summaries sync into DataEngine and are visible to get_agent_friendly_steps."""
    mock_de = Mock()
    mock_de.update_step_summary = Mock()
    mock_context.data_engine = mock_de

    summarizer = VisualStepSummarizer(mock_context)
    summarizer._llm = Mock()
    summarizer._llm.ainvoke = AsyncMock(
        return_value=AIMessage(content="[Step 1 State: Input text confirmed.]")
    )

    summarizer.dispatch(
        step_number=1,
        action_name="input_text",
        action_args={"text": "hello"},
        pre_img_bytes=b"pre",
        post_img_bytes=b"post",
        exec_outcome="Executed typing",
    )
    await summarizer.flush()

    mock_de.update_step_summary.assert_called_once_with(1, "[Step 1 State: Input text confirmed.]")


def test_draw_action_overlay_rendering():
    """Verify that draw_action_overlay_on_image correctly renders markers on images."""
    import io
    from PIL import Image
    from artemis.utils.visualization import draw_action_overlay_on_image

    # Create a blank RGB image in memory
    img = Image.new("RGB", (1080, 2400), color="white")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    raw_bytes = buf.getvalue()

    # 1. Click overlay
    annotated_click = draw_action_overlay_on_image(
        image_bytes=raw_bytes,
        action_name="click",
        action_args={"target": [540, 1200]},
    )
    assert len(annotated_click) > 0
    assert annotated_click != raw_bytes

    # 2. Swipe overlay
    annotated_swipe = draw_action_overlay_on_image(
        image_bytes=raw_bytes,
        action_name="swipe",
        action_args={"start": [500, 1800], "end": [500, 600]},
    )
    assert len(annotated_swipe) > 0
    assert annotated_swipe != raw_bytes

    # 3. Click Sequence overlay
    annotated_seq = draw_action_overlay_on_image(
        image_bytes=raw_bytes,
        action_name="click_sequence",
        action_args={"sequence": [[200, 300], [400, 500], [600, 700]]},
    )
    assert len(annotated_seq) > 0
    assert annotated_seq != raw_bytes

    # 4. Fail-safe on corrupt bytes
    corrupt_result = draw_action_overlay_on_image(
        image_bytes=b"invalid_bytes",
        action_name="click",
        action_args={"target": [100, 100]},
    )
    assert corrupt_result == b"invalid_bytes"
