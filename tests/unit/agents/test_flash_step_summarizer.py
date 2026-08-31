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

from unittest.mock import AsyncMock, Mock, call
from uuid import uuid4
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
async def test_summarizer_retries_independently(mock_context):
    """A failed summary retries without waiting for another action to arrive."""
    summarizer = VisualStepSummarizer(mock_context)
    attempt_count = 0

    async def mock_ainvoke(messages):
        nonlocal attempt_count
        attempt_count += 1
        # Fail the first attempt, then return the requested step summary.
        if attempt_count == 1:
            raise TimeoutError("Simulated timeout")
        msg_str = str(messages)
        if "Step 1" in msg_str:
            return AIMessage(content="[Step 1 State: Recovered Step 1 summary]")
        raise AssertionError("Unexpected step")

    summarizer._llm = Mock()
    summarizer._llm.ainvoke = AsyncMock(side_effect=mock_ainvoke)

    # No Step 2 dispatch is needed to trigger the retry.
    summarizer.dispatch(
        step_number=1,
        action_name="click",
        action_args={"target": [100, 100]},
        pre_img_bytes=b"pre1",
        post_img_bytes=b"post1",
        exec_outcome="Outcome 1",
    )
    await summarizer.flush()

    assert summarizer.has_summary(1)
    assert "Recovered Step 1" in summarizer.get_summary(1)
    assert attempt_count == 2


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
    assert messages[1].content[1]["text"] == (
        "--- Historical Visual Transition ---\n"
        "[Step 1 State: Tapped Search bar. Keyboard appeared.]"
    )

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


def test_context_compressor_backfills_late_summary_idempotently(mock_context):
    """A pending image remains until its late summary replaces it."""
    summarizer = VisualStepSummarizer(mock_context)
    summarizer._step_inputs["action-1"] = {"step_number": 1}
    messages = [
        HumanMessage(
            content=[
                {"type": "text", "text": "Task: Search"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,IMG_1"}},
            ]
        ),
        ToolMessage(
            tool_call_id="action-1",
            name="click",
            content=[
                {"type": "text", "text": "Tapped Search."},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,IMG_2"}},
            ],
        ),
        ToolMessage(
            tool_call_id="action-2",
            name="input_text",
            content=[
                {"type": "text", "text": "Typed query."},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,IMG_3"}},
            ],
        ),
    ]

    compress_flash_messages(messages, summarizer=summarizer)
    assert any(block["type"] == "image_url" for block in messages[1].content)
    assert len(messages[1].content) == 2

    summarizer._summaries["action-1"] = "Search field focused; keyboard appeared."
    compress_flash_messages(messages, summarizer=summarizer)
    compress_flash_messages(messages, summarizer=summarizer)

    generated_blocks = [
        block
        for block in messages[1].content
        if block.get("text", "").startswith("--- Historical Visual Transition ---")
    ]
    assert len(generated_blocks) == 1
    assert generated_blocks[0]["text"].endswith("Search field focused; keyboard appeared.")
    assert all(block["type"] != "image_url" for block in messages[1].content)


def test_context_compressor_uses_tool_call_id_over_message_ordinal(mock_context):
    """Generic tool messages cannot shift action summaries onto the wrong result."""
    summarizer = VisualStepSummarizer(mock_context)
    summarizer._summaries["click-id"] = "Click-specific visual memory."
    summarizer._summaries["type-id"] = "Typing-specific visual memory."

    messages = [
        ToolMessage(
            tool_call_id="click-id",
            name="click",
            content=[
                {"type": "text", "text": "Clicked."},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,A"}},
            ],
        ),
        ToolMessage(
            tool_call_id="explorer-id",
            name="ask_explorer",
            content=[{"type": "text", "text": "Explorer result."}],
        ),
        ToolMessage(
            tool_call_id="type-id",
            name="input_text",
            content=[
                {"type": "text", "text": "Typed."},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,B"}},
            ],
        ),
    ]

    compress_flash_messages(messages, summarizer=summarizer)

    assert "Click-specific visual memory." in str(messages[0].content)
    assert "Typing-specific visual memory." not in str(messages[0].content)
    assert "Visual Transition" not in str(messages[1].content)
    # The latest live observation remains uncompressed until a newer screen exists.
    assert messages[2].content[-1]["type"] == "image_url"


def test_context_compressor_preserves_text_before_embedded_ui_list(mock_context):
    """Pruning a combined XML block must retain its action result prefix."""
    messages = [
        ToolMessage(
            tool_call_id="old",
            name="click",
            content=[
                {
                    "type": "text",
                    "text": "Tapped Settings.\n--- UI Element List ---\n[huge tree]",
                },
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,A"}},
            ],
        ),
        ToolMessage(
            tool_call_id="live",
            name="click",
            content=[
                {"type": "text", "text": "Opened page."},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,B"}},
            ],
        ),
    ]

    compress_flash_messages(messages, prune_history_xml=True)

    assert messages[0].content == [{"type": "text", "text": "Tapped Settings."}]


@pytest.mark.asyncio
async def test_summarizer_keys_actions_independently_of_turn_number(mock_context):
    """Multiple actions from one LLM turn keep independent inputs and summaries."""
    mock_context.data_engine = Mock()
    summarizer = VisualStepSummarizer(mock_context)

    async def mock_ainvoke(messages):
        prompt = str(messages)
        if "first action" in prompt:
            return AIMessage(content="First action memory.")
        return AIMessage(content="Second action memory.")

    summarizer._llm = Mock()
    summarizer._llm.ainvoke = AsyncMock(side_effect=mock_ainvoke)
    first_step_id = uuid4()
    second_step_id = uuid4()

    summarizer.dispatch(
        step_number=1,
        action_name="click",
        action_args={"label": "first action"},
        pre_img_bytes=None,
        post_img_bytes=None,
        exec_outcome="one",
        action_key="tc-1",
        data_engine_step_id=first_step_id,
    )
    summarizer.dispatch(
        step_number=2,
        action_name="click",
        action_args={"label": "second action"},
        pre_img_bytes=None,
        post_img_bytes=None,
        exec_outcome="two",
        action_key="tc-2",
        data_engine_step_id=second_step_id,
    )
    await summarizer.flush()

    assert summarizer.get_summary("tc-1") == "First action memory."
    assert summarizer.get_summary("tc-2") == "Second action memory."
    assert summarizer._step_inputs["tc-1"]["data_engine_step_id"] == first_step_id
    assert summarizer._step_inputs["tc-2"]["data_engine_step_id"] == second_step_id
    mock_context.data_engine.update_step_summary.assert_has_calls(
        [
            call(first_step_id, "First action memory."),
            call(second_step_id, "Second action memory."),
        ],
        any_order=True,
    )


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
