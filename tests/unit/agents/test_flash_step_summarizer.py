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

from unittest.mock import ANY, AsyncMock, Mock, call
from uuid import uuid4
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from artemis.agents.flash.context_compressor import ScrubEdgeCompressor
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


@pytest.mark.asyncio
async def test_summarizer_bounded_retry_marks_failed(mock_context):
    """Persistent failures exit after 1 + retry_limit attempts with an explicit failed state."""
    mock_context.data_engine = Mock()
    summarizer = VisualStepSummarizer(mock_context, retry_limit=2)
    summarizer._retry_delays = (0.0,)
    attempt_count = 0

    async def mock_ainvoke(messages):
        nonlocal attempt_count
        attempt_count += 1
        raise TimeoutError("Simulated permanent failure")

    summarizer._llm = Mock()
    summarizer._llm.ainvoke = AsyncMock(side_effect=mock_ainvoke)

    step_id = uuid4()
    summarizer.dispatch(
        step_number=1,
        action_name="click",
        action_args={"target": [100, 100]},
        pre_img_bytes=b"pre1",
        post_img_bytes=b"post1",
        exec_outcome="Outcome 1",
        action_key="tc-fail",
        data_engine_step_id=step_id,
    )
    await summarizer.flush()

    # Bounded exit: initial attempt + retry_limit retries, then explicit failure.
    assert attempt_count == 3
    assert summarizer.has_failed("tc-fail")
    assert not summarizer.has_summary("tc-fail")
    # Lossless semantics: a failed step still reads as pending so the
    # compressor keeps its source image instead of dropping evidence.
    assert summarizer.is_pending("tc-fail")

    # DataEngine observability: pending on dispatch, failed on exhaustion.
    mock_context.data_engine.update_step_summary.assert_has_calls(
        [
            call(step_id, None, status="pending", source="visual_transition", model=ANY),
            call(step_id, None, status="failed", source="visual_transition", model=ANY),
        ]
    )


@pytest.mark.asyncio
async def test_summarizer_success_within_retry_budget_not_failed(mock_context):
    """A summary that succeeds on a retry within the budget never enters failed state."""
    summarizer = VisualStepSummarizer(mock_context, retry_limit=3)
    summarizer._retry_delays = (0.0,)
    attempt_count = 0

    async def mock_ainvoke(messages):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count < 3:
            raise TimeoutError("Transient failure")
        return AIMessage(content="[Step 1 State: Finally worked.]")

    summarizer._llm = Mock()
    summarizer._llm.ainvoke = AsyncMock(side_effect=mock_ainvoke)

    summarizer.dispatch(
        step_number=1,
        action_name="click",
        action_args={"target": [100, 100]},
        pre_img_bytes=b"pre1",
        post_img_bytes=b"post1",
        exec_outcome="Outcome 1",
        action_key="tc-ok",
    )
    await summarizer.flush()

    assert attempt_count == 3
    assert summarizer.has_summary("tc-ok")
    assert not summarizer.has_failed("tc-ok")


def test_context_compressor_with_ready_summaries(mock_context):
    """Ready summaries replace past image blocks and outdated XML is pruned.

    Rewritten against ``ScrubEdgeCompressor`` (M5; the legacy
    ``compress_flash_messages`` was removed) with ``image_scrub_depth=1`` so
    every historical message is scrubbed immediately, mirroring the legacy
    timing this test was written for. Summaries are keyed by legacy ordinals
    to keep the older-caller fallback path covered.
    """
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

    compressor = ScrubEdgeCompressor(
        summarizer=summarizer,
        prune_history_xml=True,
        image_scrub_depth=1,
        pending_grace_steps=0,
    )
    compressor.compress(messages)

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


def test_context_compressor_uses_tool_call_id_over_message_ordinal(mock_context):
    """Generic tool messages cannot shift action summaries onto the wrong result.

    (Rewritten against ``ScrubEdgeCompressor`` in M5. The legacy tests for
    pending-summary fallback and late-summary backfill were removed with
    ``compress_flash_messages``: the scrub edge covers pending handling in
    ``test_flash_scrub_edge.py``, and late backfill is now forbidden by the
    freeze invariant rather than supported.)
    """
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

    compressor = ScrubEdgeCompressor(
        summarizer=summarizer,
        image_scrub_depth=1,
        pending_grace_steps=0,
    )
    compressor.compress(messages)

    assert "Click-specific visual memory." in str(messages[0].content)
    assert "Typing-specific visual memory." not in str(messages[0].content)
    assert "Visual Transition" not in str(messages[1].content)
    # The latest live observation remains uncompressed until a newer screen exists.
    assert messages[2].content[-1]["type"] == "image_url"


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
    # M1 keying: jobs are canonical under the DataEngine step id; the
    # tool_call_id stays queryable as an alias.
    assert summarizer.get_job_payload("tc-1")["data_engine_step_id"] == first_step_id
    assert summarizer.get_job_payload("tc-2")["data_engine_step_id"] == second_step_id
    assert summarizer.resolve_key("tc-1") == str(first_step_id)
    mock_context.data_engine.update_step_summary.assert_has_calls(
        [
            call(
                first_step_id,
                "First action memory.",
                status="ready",
                source="visual_transition",
                model=ANY,
            ),
            call(
                second_step_id,
                "Second action memory.",
                status="ready",
                source="visual_transition",
                model=ANY,
            ),
        ],
        any_order=True,
    )


@pytest.mark.asyncio
async def test_single_frame_dispatch_uses_single_image_prompt_variant(mock_context):
    """A missing after-frame (every Pro step, by design) selects the
    single-image prompt: decision frame + red marker, no AFTER ACTION section."""
    summarizer = VisualStepSummarizer(mock_context)
    captured: list = []

    async def mock_ainvoke(messages):
        captured.append(messages)
        return AIMessage(
            content=(
                "In Step 1, I tapped the 'Battery' row marked in red in the"
                " Settings list, which showed 'Network & internet' and 'Display'."
            )
        )

    summarizer._llm = Mock()
    summarizer._llm.ainvoke = AsyncMock(side_effect=mock_ainvoke)

    summarizer.dispatch(
        step_number=1,
        action_name="click",
        action_args={"target": [100, 200]},
        pre_img_bytes=b"decision_frame",
        post_img_bytes=None,
        exec_outcome="executed",
    )
    await summarizer.flush()

    assert summarizer.has_summary(1)
    system_text = str(captured[0][0].content)
    human_text = str(captured[0][1].content)
    assert "Exactly ONE screenshot" in system_text
    assert "no independent post-action evidence" in system_text
    assert "DECISION FRAME" in human_text
    assert "AFTER ACTION SCREEN" not in human_text
    assert "--- [2]" not in human_text


@pytest.mark.asyncio
async def test_dual_frame_dispatch_keeps_transition_prompt(mock_context):
    """Both frames present (Flash path): the classic BEFORE/AFTER transition
    prompt and section labels are unchanged."""
    summarizer = VisualStepSummarizer(mock_context)
    captured: list = []

    async def mock_ainvoke(messages):
        captured.append(messages)
        return AIMessage(
            content=(
                "In Step 1, I tapped the 'Settings' gear icon marked in red;"
                " the main Settings menu opened showing 'Apps'."
            )
        )

    summarizer._llm = Mock()
    summarizer._llm.ainvoke = AsyncMock(side_effect=mock_ainvoke)

    summarizer.dispatch(
        step_number=1,
        action_name="click",
        action_args={"target": [100, 200]},
        pre_img_bytes=b"pre",
        post_img_bytes=b"post",
        exec_outcome="executed",
    )
    await summarizer.flush()

    assert summarizer.has_summary(1)
    system_text = str(captured[0][0].content)
    human_text = str(captured[0][1].content)
    assert "Before/After screenshots" in system_text
    assert "--- [1] BEFORE ACTION SCREEN (Action Marked Visually in Red) ---" in human_text
    assert "--- [2] AFTER ACTION SCREEN ---" in human_text
    assert "DECISION FRAME" not in human_text


@pytest.mark.asyncio
async def test_echo_output_fails_attempt_then_recovers_on_retry(mock_context):
    """An input-marker echo is a failed attempt (not a summary): the bounded
    retry regenerates and the recovered text lands normally."""
    summarizer = VisualStepSummarizer(mock_context)
    summarizer._retry_delays = (0.0,)
    attempts = 0

    async def mock_ainvoke(messages):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return AIMessage(content="--- [2] AFTER ACTION SCREEN ---")
        return AIMessage(
            content=(
                "In Step 1, I tapped the search field marked in red; the field"
                " showed placeholder text 'Search here'."
            )
        )

    summarizer._llm = Mock()
    summarizer._llm.ainvoke = AsyncMock(side_effect=mock_ainvoke)

    summarizer.dispatch(
        step_number=1,
        action_name="click",
        action_args={"target": [1, 2]},
        pre_img_bytes=b"pre",
        post_img_bytes=b"post",
        exec_outcome="executed",
    )
    await summarizer.flush()

    assert attempts == 2
    assert summarizer.has_summary(1)
    assert "Search here" in summarizer.get_summary(1)


@pytest.mark.asyncio
async def test_degenerate_outputs_exhaust_to_failed(mock_context):
    """Echo/too-short/too-long outputs never land as summaries; persistent
    degeneration exhausts the bounded retries into the explicit failed state."""
    summarizer = VisualStepSummarizer(mock_context, retry_limit=1)
    summarizer._retry_delays = (0.0,)

    async def mock_ainvoke(messages):
        text = str(messages)
        if "Step 1" in text:
            return AIMessage(content="--- [1] DECISION FRAME: SCREEN AT ACTION TIME ---")
        if "Step 2" in text:
            return AIMessage(content="x" * 2000)
        return AIMessage(content="ok.")

    summarizer._llm = Mock()
    summarizer._llm.ainvoke = AsyncMock(side_effect=mock_ainvoke)

    for step in (1, 2, 3):
        summarizer.dispatch(
            step_number=step,
            action_name="click",
            action_args={"target": [1, 2]},
            pre_img_bytes=b"pre",
            post_img_bytes=None,
            exec_outcome="executed",
        )
    await summarizer.flush()

    for step in (1, 2, 3):
        assert summarizer.has_failed(step), f"step {step} should have failed"
        assert not summarizer.has_summary(step)


@pytest.mark.asyncio
async def test_raw_lens_call_meters_llm_usage_without_touching_context_base(mock_context):
    """The raw-model lens bypass records an llm_usage trace, but its tiny
    prompt never overwrites the session's last_prompt_tokens (the compaction
    thresholds' live context base)."""
    from artemis.services.token_meter import get_meter

    engine = Mock()
    engine.current_session_id = "lens-meter-session"
    engine.current_step_id = None
    mock_context.data_engine = engine
    meter = get_meter("lens-meter-session")
    meter.last_prompt_tokens = 777

    summarizer = VisualStepSummarizer(mock_context)
    summarizer._llm = Mock()
    summarizer._llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=(
                "In Step 1, I tapped the 'Battery' row marked in red; the list"
                " showed 'Display' and 'Battery'."
            ),
            usage_metadata={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
        )
    )

    summarizer.dispatch(
        step_number=1,
        action_name="click",
        action_args={"target": [1, 2]},
        pre_img_bytes=b"pre",
        post_img_bytes=b"post",
        exec_outcome="executed",
    )
    await summarizer.flush()

    usage_calls = [
        c for c in engine.record_trace.call_args_list if c.kwargs.get("name") == "llm_usage"
    ]
    assert usage_calls, "the raw lens bypass must record an llm_usage trace"
    payload = usage_calls[0].kwargs["payload"]
    assert payload["source"].startswith("lens:visual_transition:")
    assert payload["prompt_tokens"] == 120
    assert meter.last_prompt_tokens == 777


def test_flash_config_and_builder():
    """Verify Flash profile configuration model and SDK builder fluent API."""
    cfg = Builders.AgentConfig.with_flash_config(
        max_turns=25,
        explorer_mode="flash",
        step_summarizer=True,
        step_summarizer_model="gemini-2.5-flash-lite",
        prune_history_xml=True,
    ).build()

    assert cfg.flash.max_turns == 25
    assert cfg.flash.explorer_mode == "flash"
    assert cfg.flash.step_summarizer.enabled is True
    assert cfg.flash.step_summarizer.model == "gemini-2.5-flash-lite"
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

    mock_de.update_step_summary.assert_called_once_with(
        1,
        "[Step 1 State: Input text confirmed.]",
        status="ready",
        source="visual_transition",
        model=ANY,
    )


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
