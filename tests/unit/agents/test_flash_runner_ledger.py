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

"""FlashRunner over the session transcript ledger (Pro-identical history).

Covers: the ``video_analyzer`` availability gate (same as the Pro graph) and
its prompt segment, the unbounded-by-default turn limit, the Pro-shaped
observation tail (``# CURRENT OBSERVATION [T+mm:ss]`` header, screenshot,
``--- Visible UI Elements ---`` list, ephemeral per-turn notices, the
reasoning reminder after a silent turn), and the end-to-end loop: committed
turns carry text-only tool messages and no execution-result message unless
an action failed, every recorded step of a multi-action turn is registered
on the committed turn, the final-turn tool restriction only applies to
bounded loops, and a no-tool-call turn is nudged, not terminated.
"""

import base64
import re
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from artemis.agents.flash.runner import _FINAL_TURN_WARNING, FlashRunner, _TurnRecord
from artemis.agents.operator.prompts import (
    REASONING_REMINDER,
    USER_GUIDANCE_MARKER,
    UserGuidance,
    render_user_guidance,
    render_user_instruction,
)
from artemis.agents.validator.tool_declarations import ToolExecutionResult
from artemis.context import ArtemisContext
from artemis.graph.state import State
from artemis.memory.transcript import (
    EPHEMERAL_BLOCKS_KEY,
    EXECUTION_RESULT_MARKER,
    PRO_UI_LIST_MARKER,
    TranscriptLedger,
)

OBSERVATION_HEADER_RE = re.compile(r"^# CURRENT OBSERVATION \[T\+\d{2,}:\d{2}\]$")
FAILED_RESULT_RE = re.compile(
    rf"^{re.escape(EXECUTION_RESULT_MARKER)} \(T\+\d{{2,}}:\d{{2}}\) ---\nStatus: failed\n"
)


@pytest.fixture(autouse=True)
def isolate_summarizer_model(monkeypatch):
    """Keep real summarizer behavior without constructing a provider client."""
    model = Mock()
    model.ainvoke = AsyncMock(
        side_effect=AssertionError("Configure a model response before invoking the summarizer")
    )
    factory = Mock(return_value=model)
    monkeypatch.setattr("artemis.agents.flash.summarizer.get_llm", factory)
    monkeypatch.setattr("artemis.agents.flash.summarizer.get_google_llm", factory)


@pytest.fixture
def mock_context():
    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = Mock()
    mock_llm_cfg = Mock()
    mock_llm_cfg.model = "gemini-2.5-flash"
    mock_llm_cfg.temperature = 0.1
    ctx.llm_config.get_agent.return_value = mock_llm_cfg
    ctx.device = Mock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    ctx.data_engine = None
    ctx.adb_client = None
    ctx.driver = Mock()
    return ctx


# ---------------------------------------------------------------------------
# video_analyzer availability (same gate as the Pro graph)
# ---------------------------------------------------------------------------


def test_video_analyzer_bound_only_when_recording_tools_enabled(mock_context):
    with patch("artemis.controllers.unified_controller.get_driver"):
        mock_context.execution_setup = Mock()
        mock_context.execution_setup.video_recording_tools_enabled = True
        names = [t.name for t in FlashRunner(mock_context, goal="g")._get_tools()]
        assert "video_analyzer" in names
        assert names[-1] == "report_task_status"

        mock_context.execution_setup.video_recording_tools_enabled = False
        names = [t.name for t in FlashRunner(mock_context, goal="g")._get_tools()]
        assert "video_analyzer" not in names

        # A Mock attribute (truthy but not True) must not enable the tool.
        mock_context.execution_setup = Mock()
        names = [t.name for t in FlashRunner(mock_context, goal="g")._get_tools()]
        assert "video_analyzer" not in names


HISTORY_TOOLS = {"search_history", "replay_steps", "get_step_screenshot"}


def test_history_tools_bound_only_with_a_data_engine_session(mock_context):
    with patch("artemis.controllers.unified_controller.get_driver"):
        # No DataEngine: nothing to read, the tools stay out (as in Pro).
        names = [t.name for t in FlashRunner(mock_context, goal="g")._get_tools()]
        assert not (HISTORY_TOOLS & set(names))
        prompt = FlashRunner(mock_context, goal="g")._render_system_prompt(
            FlashRunner(mock_context, goal="g")._get_tools()
        )
        assert "search_history" not in prompt and "replay_steps" not in prompt

        mock_context.data_engine = Mock()
        with patch("artemis.tools.history._recall_config", return_value=None):
            runner = FlashRunner(mock_context, goal="g")
            tools = runner._get_tools()
        names = [t.name for t in tools]
        assert HISTORY_TOOLS <= set(names)
        assert names[-1] == "report_task_status"

        # Same declarations as the LangChain tools: derived from one args schema.
        search = next(t for t in tools if t.name == "search_history")
        assert set(search.parameters["properties"]) == {"query", "step_range", "max_results"}
        assert search.parameters["required"] == []
        replay = next(t for t in tools if t.name == "replay_steps")
        assert set(replay.parameters["properties"]) == {"start_step", "end_step"}
        assert replay.parameters["required"] == ["start_step"]
        shot = next(t for t in tools if t.name == "get_step_screenshot")
        assert shot.parameters["properties"]["which"]["enum"] == ["pre", "post", "overlay"]

        prompt = runner._render_system_prompt(tools)
        assert "`search_history`" in prompt
        assert "`replay_steps`" in prompt
        assert "`get_step_screenshot`" in prompt


def test_search_history_alone_follows_the_recall_config_gate(mock_context):
    from types import SimpleNamespace

    with patch("artemis.controllers.unified_controller.get_driver"):
        mock_context.data_engine = Mock()
        with patch(
            "artemis.tools.history._recall_config",
            return_value=SimpleNamespace(enabled=False),
        ):
            names = [t.name for t in FlashRunner(mock_context, goal="g")._get_tools()]
        assert "search_history" not in names
        assert {"replay_steps", "get_step_screenshot"} <= set(names)


def test_video_analyzer_prompt_segment_follows_availability(mock_context):
    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(mock_context, goal="g")
        mock_context.execution_setup = Mock()
        mock_context.execution_setup.video_recording_tools_enabled = True
        with_video = runner._render_system_prompt(runner._get_tools())
        assert "`video_analyzer`" in with_video

        mock_context.execution_setup.video_recording_tools_enabled = False
        without_video = runner._render_system_prompt(runner._get_tools())
        assert "video_analyzer" not in without_video
        # Session-clock / history teaching is unconditional.
        assert "T+mm:ss" in without_video
        assert "Action Execution Result" in without_video


# ---------------------------------------------------------------------------
# Turn limit: unbounded by default, explicit caps honoured
# ---------------------------------------------------------------------------


def test_turn_limit_semantics(mock_context):
    with patch("artemis.controllers.unified_controller.get_driver"):
        assert FlashRunner(mock_context, goal="g", max_turns=0).turn_limit is None
        assert FlashRunner(mock_context, goal="g", max_turns=7).turn_limit == 7
        with patch("artemis.agents.flash.runner.load_agent_config", side_effect=RuntimeError):
            assert FlashRunner(mock_context, goal="g").turn_limit is None


# ---------------------------------------------------------------------------
# Observation tail: Pro shape with the session-relative header
# ---------------------------------------------------------------------------


def test_observation_tail_has_pro_shape(mock_context):
    """The objective lives in the system prompt only; the tail opens with the
    observation header on turn 1 exactly as on every later turn, and carries
    no reminder unless the previous turn was silent."""
    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(mock_context, goal="Open Settings")
        ledger = TranscriptLedger()

        tail = runner._build_tail(ledger, 1, b"IMG", "[1] Settings")
        texts = [b["text"] for b in tail.content if b["type"] == "text"]
        assert OBSERVATION_HEADER_RE.match(texts[0])
        assert texts[1] == "--- Current Screenshot ---"
        assert texts[2] == f"{PRO_UI_LIST_MARKER}\n[1] Settings"
        assert len(texts) == 3
        assert sum(1 for b in tail.content if b["type"] == "image_url") == 1
        joined = "".join(texts)
        assert "Open Settings" not in joined and "objective" not in joined.lower()
        assert "CRITICAL RULE" not in joined and REASONING_REMINDER not in joined
        assert EPHEMERAL_BLOCKS_KEY not in tail.additional_kwargs

        later = runner._build_tail(
            ledger,
            5,
            None,
            None,
            injected=UserGuidance(body='User instruction: "stop"', wrapper="[INJECTED]"),
            notices=["nudge"],
            is_final=True,
        )
        texts = [b["text"] for b in later.content]
        assert OBSERVATION_HEADER_RE.match(texts[0])
        assert "objective" not in "".join(texts).lower()
        assert texts[1:] == [
            "nudge",
            "[INJECTED]",
            'User instruction: "stop"',
            _FINAL_TURN_WARNING,
        ]


def test_per_turn_notices_are_marked_ephemeral(mock_context):
    """Nudges, the user-guidance wrapper, the final-turn warning and the
    reasoning reminder are only meaningful for the turn they were built for:
    every one of them is flagged ephemeral (by block index) so the scrub edge
    drops them before the message freezes; the observation blocks never are."""
    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(mock_context, goal="Open Settings")
    ledger = TranscriptLedger()
    guidance = render_user_guidance("stop", release_loop=False, has_plan=False)

    tail = runner._build_tail(
        ledger,
        3,
        b"IMG",
        "[1] Settings",
        notices=["nudge"],
        injected=guidance,
        is_final=True,
        previous_turn_silent=True,
    )
    texts = [b.get("text") for b in tail.content]
    # header, screenshot label, image, UI list, then the per-turn notices with
    # the persistent instruction body right after its wrapper
    assert texts[4:] == [
        "nudge",
        guidance.wrapper,
        guidance.body,
        _FINAL_TURN_WARNING,
        REASONING_REMINDER,
    ]
    assert tail.additional_kwargs[EPHEMERAL_BLOCKS_KEY] == [4, 5, 7, 8]


def test_injected_instruction_body_outlives_its_wrapper(mock_context):
    """Defect: a standing instruction ("don't send messages from now on")
    vanished after one turn because the whole guidance block was ephemeral.
    Now the wrapper is ephemeral and the verbatim body is a regular block, so
    it stays in the active window until the turn is chunked."""
    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(mock_context, goal="g")
    ledger = TranscriptLedger()
    guidance = render_user_guidance(
        "don't send messages from now on", has_plan=False, offset_label="T+01:05"
    )

    tail = runner._build_tail(ledger, 2, b"IMG", "[1] Send", injected=guidance)
    texts = [b.get("text") for b in tail.content]
    wrapper_index = texts.index(guidance.wrapper)
    body_index = texts.index(guidance.body)
    ephemeral = tail.additional_kwargs[EPHEMERAL_BLOCKS_KEY]
    assert wrapper_index in ephemeral
    assert body_index not in ephemeral
    assert body_index == wrapper_index + 1
    assert guidance.body == 'User instruction (T+01:05): "don\'t send messages from now on"'
    assert guidance.body == render_user_instruction(
        "don't send messages from now on", offset_label="T+01:05"
    )
    # The wrapper frames the quoted line but no longer carries the words.
    assert guidance.wrapper.startswith(USER_GUIDANCE_MARKER)
    assert "don't send messages" not in guidance.wrapper

    # Scrub edge parity: once the turn has left the live position (a newer
    # committed observation exists) the wrapper is gone and the body is
    # still in the committed turn.
    ledger.stage_turn([tail, AIMessage(content="ok")])
    ledger.commit_staged(step_key="s1")
    ledger.stage_turn([runner._build_tail(ledger, 3, b"IMG", "[1] Send"), AIMessage(content="ok")])
    ledger.commit_staged(step_key="s2")
    rendered = ledger.render([runner._build_tail(ledger, 4, b"IMG", "[1] Send")])
    committed = next(
        m
        for m in rendered
        if isinstance(m, HumanMessage) and guidance.body in [b.get("text") for b in m.content]
    )
    committed_texts = [b.get("text", "") for b in committed.content if isinstance(b, dict)]
    assert guidance.wrapper not in committed_texts
    assert not any(t.startswith(USER_GUIDANCE_MARKER) for t in committed_texts)


def test_reasoning_reminder_follows_a_silent_turn_only(mock_context):
    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(mock_context, goal="g")
    ledger = TranscriptLedger()

    quiet = runner._build_tail(ledger, 2, None, "[1] x", previous_turn_silent=False)
    assert REASONING_REMINDER not in [b["text"] for b in quiet.content]

    reminded = runner._build_tail(ledger, 2, None, "[1] x", previous_turn_silent=True)
    texts = [b["text"] for b in reminded.content]
    assert texts[-1] == REASONING_REMINDER
    assert texts.count(REASONING_REMINDER) == 1
    assert reminded.additional_kwargs[EPHEMERAL_BLOCKS_KEY] == [len(texts) - 1]


# ---------------------------------------------------------------------------
# Injected instruction: verbatim text for the record and the lens, wrapped
# notice for the observation tail only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injected_instruction_splits_verbatim_text_from_operator_notice(
    mock_context, tmp_path
):
    """The user's words are what the step record, the chunk ledger and the
    visual lens receive (Pro parity); the shared ``--- User Guidance ---``
    block (the Pro rendering, without a plan to edit) rides on the
    observation tail alone."""
    import json

    (tmp_path / "injected_instruction.json").write_text(
        json.dumps({"instruction": "Skip the popup and log in", "release_loop": True}),
        encoding="utf-8",
    )
    mock_context.data_engine = Mock()
    mock_context.data_engine.base_dir = str(tmp_path)

    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(mock_context, goal="Log in")
        instruction, notice = await runner._read_injected_instruction()

    assert instruction == "Skip the popup and log in"
    assert notice == render_user_guidance(
        "Skip the popup and log in", release_loop=True, has_plan=False
    )
    assert notice.wrapper.startswith(USER_GUIDANCE_MARKER)
    assert notice.body == 'User instruction: "Skip the popup and log in"'
    assert "outranks your current milestones" in notice.wrapper
    assert "task plan" not in notice.wrapper
    assert "authorized stopping any ongoing monitoring loop" in notice.wrapper
    assert "REAL-TIME INJECTED" not in notice.wrapper and "You MUST" not in notice.wrapper
    assert not (tmp_path / "injected_instruction.json").exists()

    with patch("artemis.controllers.unified_controller.get_driver"):
        assert await runner._read_injected_instruction() == (None, None)


# ---------------------------------------------------------------------------
# End-to-end loop over the ledger (mocked model / executor)
# ---------------------------------------------------------------------------


def _exec_result(tc_id, name, post_bytes, ui="[1] next"):
    return ToolExecutionResult(
        tool_call_id=tc_id,
        tool_name=name,
        status="success",
        text_summary=f"{name} executed",
        screenshot_bytes=post_bytes,
        ui_elements_text=ui,
    )


def _make_runner(mock_context, responses, max_turns=None):
    runner = FlashRunner(mock_context, goal="Open Settings", max_turns=max_turns)
    runner.summarizer = None
    runner._init_llm = Mock(return_value=Mock())
    # Snapshot each invocation: the runner appends to the same message list
    # after the call, so the recorded call args would otherwise drift.
    runner.calls = []
    pending = list(responses)

    async def _invoke(llm, tools, messages):
        runner.calls.append({"tools": list(tools), "messages": list(messages)})
        return pending.pop(0)

    runner._invoke_model = AsyncMock(side_effect=_invoke)
    runner.executor.execute = AsyncMock(
        side_effect=lambda name, args, tc_id, state, **_: _exec_result(
            tc_id, name, f"IMG-{tc_id}".encode()
        )
    )
    return runner


def _report(status="completed", tc_id="tc-report", **extra):
    return AIMessage(
        content="Done.",
        tool_calls=[
            {"name": "report_task_status", "args": {"status": status, **extra}, "id": tc_id}
        ],
    )


_INITIAL_OBSERVATION = ("shot0.png", b"IMG0", "[1] Settings")


@pytest.mark.asyncio
async def test_run_builds_prompt_from_ledger_with_session_offsets(mock_context):
    """Turn 2 must see: the static system prefix, the committed turn 1 (its
    tail, the AI message, text-only tool messages that already carry each
    action's outcome — no execution-result message repeats them) and a fresh
    observation tail carrying the last action's post screenshot — the Pro
    transcript shape, with no cap."""
    responses = [
        AIMessage(
            content="I will tap then wait.",
            tool_calls=[
                {
                    "name": "click",
                    "args": {"target": [500, 600], "target_description": "Settings"},
                    "id": "tc1",
                },
                {"name": "wait_for_delay", "args": {"seconds": 1}, "id": "tc2"},
            ],
        ),
        _report(explanation="ok"),
    ]
    with (
        patch("artemis.controllers.unified_controller.get_driver"),
        patch(
            "artemis.agents.flash.runner.capture_screenshot_and_parse_ui",
            AsyncMock(return_value=_INITIAL_OBSERVATION),
        ),
    ):
        runner = _make_runner(mock_context, responses, max_turns=0)
        assert runner.turn_limit is None
        result = await runner.run(State(initial_goal="Open Settings"))

    assert result == {"status": "completed", "explanation": "ok"}
    assert runner._invoke_model.await_count == 2

    turn2_messages = runner.calls[1]["messages"]
    assert isinstance(turn2_messages[0], SystemMessage)
    assert "T+mm:ss" in turn2_messages[0].content

    tail1 = turn2_messages[1]
    assert isinstance(tail1, HumanMessage)
    tail1_texts = [b["text"] for b in tail1.content if b["type"] == "text"]
    assert OBSERVATION_HEADER_RE.match(tail1_texts[0])
    assert "objective" not in "".join(tail1_texts).lower()
    assert "Open Settings" in turn2_messages[0].content  # stated once, in the prefix

    assert isinstance(turn2_messages[2], AIMessage)
    tool_msgs = [m for m in turn2_messages if isinstance(m, ToolMessage)]
    assert [m.content for m in tool_msgs] == ["click executed", "wait_for_delay executed"]

    # Every action succeeded: the tool messages are the outcome, nothing repeats them.
    assert not any(
        isinstance(m, HumanMessage)
        and any(EXECUTION_RESULT_MARKER in b.get("text", "") for b in m.content)
        for m in turn2_messages
    )

    tail2 = turn2_messages[5]
    tail2_texts = [b["text"] for b in tail2.content if b["type"] == "text"]
    assert OBSERVATION_HEADER_RE.match(tail2_texts[0])
    assert "objective" not in "".join(tail2_texts).lower()
    images = [b for b in tail2.content if b["type"] == "image_url"]
    assert len(images) == 1
    assert images[0]["image_url"]["url"].endswith(base64.b64encode(b"IMG-tc2").decode())
    assert tail2_texts[1] == "--- Current Screenshot ---"
    assert tail2_texts[2] == f"{PRO_UI_LIST_MARKER}\n[1] next"
    assert len(turn2_messages) == 6

    # Both actions of the multi-action turn were registered on the committed turn.
    ledger = mock_context.transcript_ledger
    assert ledger.unchunked_turns()[0]["step_keys"] == ["tc1", "tc2"]


@pytest.mark.asyncio
async def test_run_failed_action_turn_ends_with_the_error_result(mock_context):
    """A failed action is the one case worth a result message: it carries the
    error the turn ended on, after the tool message."""
    responses = [
        AIMessage(
            content="Tapping.",
            tool_calls=[{"name": "click", "args": {"target": 99}, "id": "tc1"}],
        ),
        _report(),
    ]
    with (
        patch("artemis.controllers.unified_controller.get_driver"),
        patch(
            "artemis.agents.flash.runner.capture_screenshot_and_parse_ui",
            AsyncMock(return_value=_INITIAL_OBSERVATION),
        ),
    ):
        runner = _make_runner(mock_context, responses, max_turns=0)
        runner.executor.execute = AsyncMock(
            return_value=ToolExecutionResult(
                tool_call_id="tc1",
                tool_name="click",
                status="error",
                text_summary="Error during click: Invalid target index 99.",
            )
        )
        runner._capture_post_screenshot = AsyncMock(return_value=None)
        await runner.run(State(initial_goal="Open Settings"))

    turn2_messages = runner.calls[1]["messages"]
    result_msgs = [
        m
        for m in turn2_messages
        if isinstance(m, HumanMessage)
        and any(EXECUTION_RESULT_MARKER in b.get("text", "") for b in m.content)
    ]
    assert len(result_msgs) == 1
    text = result_msgs[0].content[0]["text"]
    assert FAILED_RESULT_RE.match(text)
    assert "Invalid target index 99" in text
    assert isinstance(turn2_messages[turn2_messages.index(result_msgs[0]) - 1], ToolMessage)


@pytest.mark.asyncio
async def test_run_reminds_after_a_silent_turn_without_a_bounce(mock_context):
    """The ledger judges the committed turn (sibling logic, authoritative
    here); the runner only reads ``last_turn_silent`` when it builds the
    next tail: one reminder block, marked ephemeral, one model call per turn."""
    responses = [
        AIMessage(
            content="",  # bare tool call, no visible reasoning
            tool_calls=[
                {"name": "click", "args": {"target": 1}, "id": "tc1"},
            ],
        ),
        _report(),
    ]
    with (
        patch("artemis.controllers.unified_controller.get_driver"),
        patch(
            "artemis.agents.flash.runner.capture_screenshot_and_parse_ui",
            AsyncMock(return_value=_INITIAL_OBSERVATION),
        ),
        patch.object(TranscriptLedger, "last_turn_silent", new_callable=PropertyMock) as silent,
    ):
        silent.side_effect = lambda: silent.call_count > 1  # turn 1: False, later: True
        runner = _make_runner(mock_context, responses, max_turns=0)
        await runner.run(State(initial_goal="Open Settings"))

    assert runner._invoke_model.await_count == 2
    tail1 = runner.calls[0]["messages"][-1]
    assert REASONING_REMINDER not in [b.get("text") for b in tail1.content]
    tail2 = runner.calls[1]["messages"][-1]
    texts = [b.get("text") for b in tail2.content]
    assert texts.count(REASONING_REMINDER) == 1
    assert tail2.additional_kwargs[EPHEMERAL_BLOCKS_KEY] == [texts.index(REASONING_REMINDER)]


@pytest.mark.asyncio
async def test_run_calibrates_the_ledger_only_from_measured_usage(mock_context):
    """A provider-reported prompt size calibrates the chars-per-token ratio
    with the sent messages; an estimated size (no usage metadata) records the
    base but never calibrates — the estimate is itself derived from character
    counts, so calibrating on it would be circular."""
    measured = AIMessage(
        content="Tapping.",
        tool_calls=[{"name": "click", "args": {"target": 1}, "id": "tc1"}],
        usage_metadata={"input_tokens": 1200, "output_tokens": 5, "total_tokens": 1205},
    )
    responses = [measured, _report()]
    with (
        patch("artemis.controllers.unified_controller.get_driver"),
        patch(
            "artemis.agents.flash.runner.capture_screenshot_and_parse_ui",
            AsyncMock(return_value=_INITIAL_OBSERVATION),
        ),
        patch.object(TranscriptLedger, "record_prompt_tokens") as record,
    ):
        runner = _make_runner(mock_context, responses, max_turns=0)
        await runner.run(State(initial_goal="Open Settings"))

    assert record.call_count == 2
    first_call, second_call = record.call_args_list
    assert first_call.args[0] == 1200 and first_call.kwargs["messages"] is not None
    # The report turn carries no usage metadata: estimated, recorded, not calibrated.
    assert second_call.args[0] >= 1 and second_call.kwargs["messages"] is None


@pytest.mark.asyncio
async def test_run_final_turn_restricts_tools_and_fails_without_report(mock_context):
    responses = [AIMessage(content="I give up.", tool_calls=[])]
    with (
        patch("artemis.controllers.unified_controller.get_driver"),
        patch(
            "artemis.agents.flash.runner.capture_screenshot_and_parse_ui",
            AsyncMock(return_value=_INITIAL_OBSERVATION),
        ),
    ):
        runner = _make_runner(mock_context, responses, max_turns=1)
        result = await runner.run(State(initial_goal="Open Settings"))

    assert result == {"status": "failed", "explanation": "I give up."}
    tools = runner.calls[0]["tools"]
    assert [t.name for t in tools] == ["report_task_status"]
    tail = runner.calls[0]["messages"][-1]
    assert any("final turn" in b.get("text", "") for b in tail.content)


@pytest.mark.asyncio
async def test_run_no_tool_call_turn_is_nudged_not_terminated(mock_context):
    responses = [AIMessage(content="thinking only", tool_calls=[]), _report()]
    with (
        patch("artemis.controllers.unified_controller.get_driver"),
        patch(
            "artemis.agents.flash.runner.capture_screenshot_and_parse_ui",
            AsyncMock(return_value=_INITIAL_OBSERVATION),
        ),
    ):
        runner = _make_runner(mock_context, responses, max_turns=0)
        result = await runner.run(State(initial_goal="Open Settings"))

    assert result == {"status": "completed"}
    turn2_messages = runner.calls[1]["messages"]
    # Unbounded loop: no final-turn restriction was ever applied.
    tools = runner.calls[1]["tools"]
    assert "click" in [t.name for t in tools]
    tail2_texts = [b["text"] for b in turn2_messages[-1].content if b["type"] == "text"]
    assert any("did not call any tools" in t for t in tail2_texts)
    # A helper-only / empty turn commits without an execution-result message.
    assert not any(
        isinstance(m, HumanMessage)
        and any(EXECUTION_RESULT_MARKER in b.get("text", "") for b in m.content)
        for m in turn2_messages
    )


# ---------------------------------------------------------------------------
# _TurnRecord.result: the turn's execution result in the validator-report shape
# ---------------------------------------------------------------------------


def test_turn_record_result_is_none_for_helper_only_turns():
    assert _TurnRecord().result() is None


def test_turn_record_result_is_none_when_every_action_succeeded():
    """The tool messages already say ``Swiped up.``; a ``Status: dispatched``
    line after them would only duplicate the outcome (and put two human
    messages back to back)."""
    record = _TurnRecord(
        actions=[("click", "success", "Tapped at [500, 500]"), ("swipe", "success", "Swiped up.")]
    )
    assert record.result() is None


def test_turn_record_failed_result_keeps_an_executor_message_that_names_the_action():
    """Executor messages already name the action; they are not prefixed again."""
    record = _TurnRecord(
        actions=[
            ("click", "success", "Tapped at [500, 500]"),
            ("swipe", "error", "Error executing swipe: direction 'sideways' is not valid"),
        ]
    )
    assert record.result() == {
        "status": "failed",
        "error": "Error executing swipe: direction 'sideways' is not valid",
    }


def test_turn_record_failed_result_prefixes_an_error_that_does_not_name_the_action():
    record = _TurnRecord(actions=[("type_text", "error", "Device is offline")])
    assert record.result() == {"status": "failed", "error": "type_text: Device is offline"}


def test_turn_record_reports_the_first_failure_of_a_multi_action_turn():
    record = _TurnRecord(
        actions=[
            ("click", "error", "click rejected: coordinates out of bounds"),
            ("swipe", "error", "never reached"),
        ]
    )
    assert record.result()["error"] == "click rejected: coordinates out of bounds"
