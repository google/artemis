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

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
import pytest

from artemis.agents.checker.checker import (
    CheckReport,
    CheckVerdict,
    _normalize_report,
    _run_check_loop,
    assemble_checker_prompt_segments,
    build_checker_tools,
    build_probe_argv,
    run_checkpoint_check,
    run_final_check,
    verdicts_allow_release,
)
from artemis.context import ArtemisContext, ExecutionSetup
from artemis.core.tool_failure import ToolFailure
from artemis.graph.checkpoints import EvidenceAnchor
from artemis.utils.plan_grammar import CheckItem


def _ci(kind="verify", when="on_complete", text="expected state", parent="p"):
    return CheckItem(kind=kind, when=when, text=text, parent_key=parent)


def _mock_ctx(**setup_kwargs):
    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = ExecutionSetup(**setup_kwargs)
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = "unused"
    ctx.data_engine.get_agent_friendly_steps.return_value = []
    ctx.data_engine.get_step_number.return_value = 7
    return ctx


# --- probe_device: enumerated table, programmatic argv (§8 item 12) ------------------


def test_probe_argv_enumerated_kinds_allowed():
    assert build_probe_argv("alarms") == ["dumpsys", "alarm"]
    assert build_probe_argv("battery") == ["dumpsys", "battery"]
    assert build_probe_argv("foreground") == ["dumpsys", "activity", "activities"]
    assert build_probe_argv("packages") == ["pm", "list", "packages"]
    assert build_probe_argv("setting", {"namespace": "system", "key": "screen_brightness"}) == [
        "settings",
        "get",
        "system",
        "screen_brightness",
    ]
    assert build_probe_argv("content", {"uri": "content://settings/system"}) == [
        "content",
        "query",
        "--uri",
        "content://settings/system",
    ]
    assert build_probe_argv("prop", {"key": "ro.build.version.sdk"}) == [
        "getprop",
        "ro.build.version.sdk",
    ]


def test_probe_argv_rejects_unknown_kind():
    with pytest.raises(ValueError):
        build_probe_argv("shell")
    with pytest.raises(ValueError):
        build_probe_argv("dumpsys")


def test_probe_argv_rejects_whitespace_and_metachars():
    for bad in ("a key", "key;rm", "key|x", "key$", "key`x`", "key&&y", "a\nb"):
        with pytest.raises(ValueError):
            build_probe_argv("prop", {"key": bad})
    with pytest.raises(ValueError):
        build_probe_argv("setting", {"namespace": "system", "key": "a b"})
    with pytest.raises(ValueError):
        build_probe_argv("content", {"uri": "content://a; rm -rf /"})


def test_probe_argv_battery_set_inexpressible():
    """Mutating dumpsys subcommands cannot be expressed: no-parameter probes
    reject every parameter."""
    with pytest.raises(ValueError):
        build_probe_argv("battery", {"extra": "set level 100"})
    with pytest.raises(ValueError):
        build_probe_argv("alarms", {"args": "anything"})
    # And the setting namespace is a closed set
    with pytest.raises(ValueError):
        build_probe_argv("setting", {"namespace": "battery", "key": "level"})


# --- Release decision is node-side and never rewrites verdicts -----------------------


def test_verdicts_allow_release_semantics():
    ok = CheckReport(
        verdicts=[
            CheckVerdict(item_text="a", kind="verify", status="passed", evidence="e"),
            CheckVerdict(item_text="b", kind="verify", status="inconclusive", evidence="e"),
            CheckVerdict(item_text="c", kind="assert", status="failed", evidence="e"),
        ]
    )
    # Assert failures never block release
    assert verdicts_allow_release(ok)

    blocked = CheckReport(
        verdicts=[
            CheckVerdict(item_text="a", kind="verify", status="failed", evidence="e"),
        ]
    )
    assert not verdicts_allow_release(blocked)


def test_normalize_report_downgrades_vague_failures_and_fills_missing():
    items = [_ci(kind="verify", text="v1"), _ci(kind="assert", text="a1")]
    report = CheckReport(
        verdicts=[CheckVerdict(item_text="v1", kind="verify", status="failed", evidence="  ")]
    )
    normalized = _normalize_report(report, items)
    by_text = {(v.kind, v.item_text): v for v in normalized.verdicts}
    # Vague failed -> inconclusive (verdict value hygiene, not release logic)
    assert by_text[("verify", "v1")].status == "inconclusive"
    # Missing item gets an inconclusive verdict, never a silent pass
    assert by_text[("assert", "a1")].status == "inconclusive"


# --- Assembly: prompt segments x entry x content x config (§8 item 18) ---------------


def test_prompt_segments_verify_only_has_no_assert_section():
    prompts = {
        "base_rules": "BASE",
        "verify_semantics": "VERIFY-SEG",
        "assert_semantics": "ASSERT-SEG",
        "anchor_guide": "ANCHOR-SEG",
        "final_guide": "FINAL-SEG",
        "probe_guide": "PROBE-SEG",
    }
    text = assemble_checker_prompt_segments(
        "checkpoint", [_ci(kind="verify")], probe_tool_registered=True, prompts=prompts
    )
    assert "VERIFY-SEG" in text
    assert "ASSERT-SEG" not in text
    assert "ANCHOR-SEG" in text
    assert "FINAL-SEG" not in text
    assert "PROBE-SEG" in text

    text2 = assemble_checker_prompt_segments(
        "final", [_ci(kind="assert")], probe_tool_registered=False, prompts=prompts
    )
    assert "ASSERT-SEG" in text2
    assert "VERIFY-SEG" not in text2
    assert "FINAL-SEG" in text2
    assert "ANCHOR-SEG" not in text2
    # Probe disabled -> zero mention of probing anywhere in the prompt
    assert "PROBE-SEG" not in text2


def test_tool_table_probe_gate():
    ctx = _mock_ctx(disable_device_probes=False)
    names = {t.name for t in build_checker_tools(ctx, "checkpoint")}
    assert "probe_device" in names
    assert "read_note" in names
    # The shared history tools, mounted as a set.
    assert {"search_history", "replay_steps", "get_step_screenshot"} <= names
    # Never any device action, note write, or sub-agent
    assert not names & {
        "save_note",
        "update_note",
        "append_note",
        "click",
        "swipe",
        "ask_diagnoser",
        "ask_explorer",
        "run_adb_command",
    }

    ctx2 = _mock_ctx(disable_device_probes=True)
    names2 = {t.name for t in build_checker_tools(ctx2, "checkpoint")}
    assert "probe_device" not in names2


@pytest.mark.asyncio
async def test_checkpoint_entry_never_touches_live_screen():
    """Evidence discipline: the checkpoint entry must not capture the current
    screen — its evidence is anchored history plus persistent probes."""
    ctx = _mock_ctx()
    report = CheckReport(
        verdicts=[CheckVerdict(item_text="x", kind="verify", status="passed", evidence="e")]
    )

    structured = MagicMock()
    structured.ainvoke = AsyncMock(return_value=report)
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    response = MagicMock()
    response.tool_calls = []

    with (
        patch("artemis.agents.checker.checker.get_llm", return_value=llm),
        patch(
            "artemis.agents.checker.checker.acomplete",
            new=AsyncMock(return_value=response),
        ),
        patch(
            "artemis.agents.checker.checker._capture_final_screen",
            new=AsyncMock(return_value=(None, "SHOULD NOT BE CALLED")),
        ) as capture_mock,
    ):
        result = await run_checkpoint_check(
            ctx,
            check_items=[_ci(text="x")],
            anchor=EvidenceAnchor(anchor_step_id="sid", trigger_ts=0.0, plan_text="- [x] G"),
            goal="the goal",
            subgoal_text="G",
        )

    capture_mock.assert_not_awaited()
    assert result.verdicts[0].status == "passed"


@pytest.mark.asyncio
async def test_final_entry_captures_live_screen():
    ctx = _mock_ctx()
    report = CheckReport(verdicts=[])

    structured = MagicMock()
    structured.ainvoke = AsyncMock(return_value=report)
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    response = MagicMock()
    response.tool_calls = []

    with (
        patch("artemis.agents.checker.checker.get_llm", return_value=llm),
        patch(
            "artemis.agents.checker.checker.acomplete",
            new=AsyncMock(return_value=response),
        ),
        patch(
            "artemis.agents.checker.checker._capture_final_screen",
            new=AsyncMock(return_value=("b64img", "elements")),
        ) as capture_mock,
    ):
        await run_final_check(
            ctx,
            goal="the goal",
            plan_text="- [x] G",
            ledger=[],
            check_items=[_ci(kind="assert", when="at_end", text="no crash", parent=None)],
        )

    capture_mock.assert_awaited_once()


# --- Shared history tools in the check loop ------------------------------------------


def _jpeg_file(tmp_path, color="white"):
    from PIL import Image

    path = tmp_path / "pre.jpg"
    Image.new("RGB", (200, 400), color).save(path, format="JPEG")
    return path


def _screenshot_then_verdict(provider, tool_call=None):
    """LLM double: first turn asks for a step screenshot (or ``tool_call``),
    second turn answers."""
    from types import SimpleNamespace

    report = CheckReport(
        verdicts=[CheckVerdict(item_text="x", kind="verify", status="passed", evidence="e")]
    )
    structured = MagicMock()
    structured.ainvoke = AsyncMock(return_value=report)
    llm = MagicMock()
    llm.endpoint = SimpleNamespace(provider=provider)
    llm.with_structured_output.return_value = structured

    ask = MagicMock()
    ask.tool_calls = [
        tool_call
        or {
            "name": "get_step_screenshot",
            "args": {"step_number": 2, "which": "pre"},
            "id": "tc-shot",
        }
    ]
    done = MagicMock()
    done.tool_calls = []
    return llm, AsyncMock(side_effect=[ask, done])


async def _run_with_screenshot(tmp_path, provider):
    path = _jpeg_file(tmp_path)
    ctx = _mock_ctx()
    ctx.data_engine.get_step_image_path.return_value = path
    llm, acomplete = _screenshot_then_verdict(provider)

    with (
        patch("artemis.agents.checker.checker.get_llm", return_value=llm),
        patch("artemis.agents.checker.checker.acomplete", new=acomplete),
    ):
        report = await run_checkpoint_check(
            ctx,
            check_items=[_ci(text="x")],
            anchor=EvidenceAnchor(anchor_step_id="sid", trigger_ts=0.0, plan_text="- [x] G"),
            goal="the goal",
            subgoal_text="G",
        )
    ctx.data_engine.get_step_image_path.assert_called_once_with(2, "pre")
    # The message list handed to the second model call carries the tool result.
    messages = acomplete.call_args_list[1][0][1]
    return report, messages


@pytest.mark.asyncio
async def test_step_screenshot_rides_inside_the_tool_message_for_gemini(tmp_path):
    from langchain_core.messages import HumanMessage, ToolMessage

    report, messages = await _run_with_screenshot(tmp_path, "google")
    assert report.verdicts[0].status == "passed"

    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "tc-shot"
    assert tool_msgs[0].name == "get_step_screenshot"
    assert tool_msgs[0].status == "success"
    blocks = tool_msgs[0].content
    assert blocks[0]["text"] == "Screenshot of step 2 (pre-action) is attached."
    assert blocks[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    # No extra human turn was spliced in for this provider.
    assert sum(isinstance(m, HumanMessage) for m in messages) == 1


@pytest.mark.asyncio
async def test_step_screenshot_follows_as_a_human_message_for_openai(tmp_path):
    from langchain_core.messages import HumanMessage, ToolMessage

    _, messages = await _run_with_screenshot(tmp_path, "openai")
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content == "Screenshot of step 2 (pre-action) is attached."
    follower = messages[messages.index(tool_msgs[0]) + 1]
    assert isinstance(follower, HumanMessage)
    assert follower.content[0]["text"] == "[Screenshot returned by get_step_screenshot for step 2]"
    assert follower.content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_replay_steps_result_is_a_plain_tool_message(tmp_path):
    from langchain_core.messages import ToolMessage

    ctx = _mock_ctx()
    ctx.data_engine.get_agent_friendly_steps_in_range.return_value = [
        {
            "step_id": "s3",
            "step_number": 3,
            "relative_time": "9.0s",
            "summary": "Saved the alarm.",
            "action_taken": {"action": "click", "target": [0.5, 0.5], "target_text": "Save"},
            "last_execution_result": {"status": "success"},
            "interleaved_events": [],
        }
    ]
    llm, acomplete = _screenshot_then_verdict(
        "google", {"name": "replay_steps", "args": {"start_step": 3}, "id": "tc-replay"}
    )
    with (
        patch("artemis.agents.checker.checker.get_llm", return_value=llm),
        patch("artemis.agents.checker.checker.acomplete", new=acomplete),
    ):
        await run_checkpoint_check(
            ctx,
            check_items=[_ci(text="x")],
            anchor=EvidenceAnchor(anchor_step_id="sid", trigger_ts=0.0, plan_text="- [x] G"),
            goal="the goal",
            subgoal_text="G",
        )
    ctx.data_engine.get_agent_friendly_steps_in_range.assert_called_once_with(3, 3)
    messages = acomplete.call_args_list[1][0][1]
    tool_msg = next(m for m in messages if isinstance(m, ToolMessage))
    assert tool_msg.name == "replay_steps"
    assert "- **Step 3 (Start: 9.0s)**" in tool_msg.content
    assert "[Screen]: Saved the alarm." in tool_msg.content
    assert "[Planned Action]: Tapped 'Save' at [0.5, 0.5]" in tool_msg.content


# --- Streamed-reasoning transcript (replay of the live Thought/Work text) -------------


class _FakeBus:
    """Minimal DataEngine bus: subscribers receive every published event."""

    def __init__(self):
        self.subscribers = []

    def subscribe(self, cb):
        self.subscribers.append(cb)

    def unsubscribe(self, cb):
        self.subscribers.remove(cb)

    def _publish(self, event_type, data):
        for cb in list(self.subscribers):
            cb(event_type, data)


def _bus_ctx(tmp_path):
    """Mock engine whose bus actually delivers events (the rest stays mocked)."""
    ctx = _mock_ctx()
    ctx.data_engine.base_dir = tmp_path
    bus = _FakeBus()
    ctx.data_engine.subscribers = bus.subscribers
    ctx.data_engine.subscribe = bus.subscribe
    ctx.data_engine.unsubscribe = bus.unsubscribe
    ctx.data_engine._publish = bus._publish
    return ctx


def _stream(ctx, exec_id, chunk, *, stream_type="text", parent=None):
    from artemis.data_engine.context_vars import CURRENT_TRACE_ID

    ctx.data_engine._publish(
        "llm_stream",
        {
            "execution_id": exec_id,
            "parent_trace_id": parent or str(CURRENT_TRACE_ID.get()),
            "chunk": chunk,
            "stream_type": stream_type,
        },
    )


def _read_streams(tmp_path):
    from artemis.graph.checkpoints import read_attempt_streams

    return read_attempt_streams(tmp_path)


@pytest.mark.asyncio
async def test_checker_persists_its_streamed_turns_as_timestamped_segments(tmp_path):
    """Every llm_stream chunk emitted under the Checker's own trace is kept as
    one segment per (turn, kind) in first-chunk order; other agents' streams
    and a discarded (mid-stream reset) turn are left out."""
    ctx = _bus_ctx(tmp_path)

    async def loop(ctx, messages, tools, items):
        _stream(ctx, "turn-1", "Looking at ", stream_type="thinking")
        _stream(ctx, "turn-1", "step 2.", stream_type="thinking")
        _stream(ctx, "turn-1", "Let me replay it.")
        _stream(ctx, "operator-turn", "not mine", parent="someone-else")
        _stream(ctx, "turn-2", "partial ")
        ctx.data_engine._publish(
            "llm_stream_reset", {"stream_exec_id": "turn-2", "action": "discard"}
        )
        _stream(ctx, "turn-3", "The alarm is there.")
        return CheckReport(verdicts=[])

    with (
        patch("artemis.agents.checker.checker._run_check_loop", side_effect=loop),
        patch("artemis.agents.checker.checker.build_checker_tools", return_value=[]),
    ):
        await run_checkpoint_check(
            ctx,
            check_items=[_ci(text="x")],
            anchor=EvidenceAnchor(anchor_step_id="sid", trigger_ts=0.0, plan_text="- [x] G"),
            goal="the goal",
            subgoal_text="G",
            attempt_id="abc#1",
        )

    assert ctx.data_engine.subscribers == []  # detached after the run
    records = _read_streams(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["attempt_id"] == "abc#1"
    assert rec["checkpoint_id"] == "abc"
    assert rec["phase"] == "checkpoint"
    assert rec["trace_id"]
    assert rec["truncated"] is False and rec["dropped_chars"] == 0
    assert isinstance(rec["ts"], float)
    segs = rec["segments"]
    assert [(s["execution_id"], s["role"], s["text"]) for s in segs] == [
        ("turn-1", "thought", "Looking at step 2."),
        ("turn-1", "answer", "Let me replay it."),
        ("turn-3", "answer", "The alarm is there."),
    ]
    assert all(isinstance(s["when"], float) for s in segs)
    assert segs[0]["when"] <= segs[1]["when"] <= segs[2]["when"]


@pytest.mark.asyncio
async def test_final_entry_persists_transcript_even_when_the_loop_fails(tmp_path):
    """The transcript is written in a ``finally``: an attempt that times out,
    raises or is superseded still leaves what it said."""
    ctx = _bus_ctx(tmp_path)

    async def loop(ctx, messages, tools, items):
        _stream(ctx, "turn-1", "Checking the final screen…")
        raise RuntimeError("provider down")

    with (
        patch("artemis.agents.checker.checker._run_check_loop", side_effect=loop),
        patch("artemis.agents.checker.checker.build_checker_tools", return_value=[]),
        patch(
            "artemis.agents.checker.checker._capture_final_screen",
            AsyncMock(return_value=(None, "")),
        ),
        pytest.raises(RuntimeError),
    ):
        await run_final_check(
            ctx,
            goal="g",
            plan_text="- [x] G",
            ledger=[],
            check_items=[_ci(text="x")],
            attempt_id="final#1",
        )

    records = _read_streams(tmp_path)
    assert [r["attempt_id"] for r in records] == ["final#1"]
    assert records[0]["phase"] == "final" and records[0]["checkpoint_id"] == "final"
    assert records[0]["segments"][0]["text"] == "Checking the final screen…"


@pytest.mark.asyncio
async def test_no_transcript_without_attempt_id_or_streamed_text(tmp_path):
    ctx = _bus_ctx(tmp_path)

    async def silent(ctx, messages, tools, items):
        return CheckReport(verdicts=[])

    async def talkative(ctx, messages, tools, items):
        _stream(ctx, "turn-1", "text")
        return CheckReport(verdicts=[])

    with patch("artemis.agents.checker.checker.build_checker_tools", return_value=[]):
        with patch("artemis.agents.checker.checker._run_check_loop", side_effect=talkative):
            await run_checkpoint_check(
                ctx,
                check_items=[_ci(text="x")],
                anchor=EvidenceAnchor(anchor_step_id="sid", trigger_ts=0.0, plan_text=""),
                goal="g",
                subgoal_text="G",
            )
        with patch("artemis.agents.checker.checker._run_check_loop", side_effect=silent):
            await run_checkpoint_check(
                ctx,
                check_items=[_ci(text="x")],
                anchor=EvidenceAnchor(anchor_step_id="sid", trigger_ts=0.0, plan_text=""),
                goal="g",
                subgoal_text="G",
                attempt_id="abc#2",
            )

    assert _read_streams(tmp_path) == []
    assert not (tmp_path / "check_streams.jsonl").exists()


# --- Probe failures are structural; history rows keep target provenance ---------------


@pytest.mark.asyncio
async def test_probe_device_invalid_kind_is_a_tool_failure():
    """A refused probe answers with a ToolFailure, so the tool loop marks the
    ToolMessage as an error structurally rather than by sniffing its wording."""
    from artemis.agents.checker.checker import get_probe_tool
    from artemis.core.tool_failure import is_tool_failure

    tool = get_probe_tool(_mock_ctx(disable_device_probes=False))
    result = await tool.ainvoke({"kind": "shell", "params": None})
    assert is_tool_failure(result)
    assert "Error" in str(result)


@pytest.mark.asyncio
async def test_probe_device_execution_error_is_a_tool_failure():
    from artemis.agents.checker.checker import get_probe_tool
    from artemis.core.tool_failure import is_tool_failure

    ctx = _mock_ctx(disable_device_probes=False)
    ctx.get_adb_client.side_effect = RuntimeError("no adb")
    tool = get_probe_tool(ctx)
    result = await tool.ainvoke({"kind": "battery", "params": None})
    assert is_tool_failure(result)
    assert "no adb" in str(result)


def test_format_history_marks_self_described_targets_only():
    """A step without a summary renders its action through the shared renderer,
    so the Checker sees which target labels are the model's own statement."""
    from artemis.agents.checker.checker import _format_history
    from artemis.utils.task_tree import SELF_DESCRIBED_MARKER

    ctx = _mock_ctx()
    ctx.data_engine.get_agent_friendly_steps.return_value = [
        {
            "step_number": 1,
            "relative_time": "T+00:01",
            "summary": "",
            "action_taken": {
                "action": "click",
                "coordinates": [500, 600],
                "target_description": "play button",
            },
        },
        {
            "step_number": 2,
            "relative_time": "T+00:05",
            "summary": None,
            "action_taken": {
                "action": "click",
                "coordinates": [100, 200],
                "target_text": "Login",
                "target_resource_id": "com.app:id/login",
            },
        },
    ]

    text = _format_history(ctx)
    lines = text.splitlines()
    assert lines[0].startswith("- Step 1 (T+00:01): ")
    assert f"'play button' {SELF_DESCRIBED_MARKER}" in lines[0]
    assert lines[1].startswith("- Step 2 (T+00:05): ")
    assert "'Login'" in lines[1]
    assert SELF_DESCRIBED_MARKER not in lines[1]
    # Neither row is the raw JSON record.
    assert "target_description" not in text
    assert "target_resource_id" not in text


# --- tool results: status is structural, never sniffed from the words -----------------


# A helper tool reports failure structurally (``ToolFailure``); free-form text
# that merely starts with "Error" is an ordinary answer.
_STATUS_CASES = [
    pytest.param(ToolFailure("Error: note 'progress' not found"), "error", id="tool_failure"),
    pytest.param("Error 404 was typed into the search box", "success", id="plain_error_text"),
]


def _text_tool(name: str, result):
    async def _run(key: str) -> str:
        return result

    return StructuredTool.from_function(coroutine=_run, name=name, description=name)


@pytest.mark.asyncio
@pytest.mark.parametrize("result, expected_status", _STATUS_CASES)
async def test_check_loop_tool_message_status_is_structural(result, expected_status):
    ctx = _mock_ctx()
    tool = _text_tool("read_note", result)
    tool_turn = AIMessage(
        content="", tool_calls=[{"name": "read_note", "args": {"key": "progress"}, "id": "c1"}]
    )
    final_turn = AIMessage(content="done", tool_calls=[])

    llm = MagicMock()
    llm.bind_tools.return_value = llm
    llm.with_structured_output.return_value.ainvoke = AsyncMock(
        return_value=CheckReport(verdicts=[])
    )
    messages = [SystemMessage(content="s"), HumanMessage(content="h")]

    with (
        patch("artemis.agents.checker.checker.get_llm", return_value=llm),
        patch(
            "artemis.agents.checker.checker.acomplete",
            new=AsyncMock(side_effect=[tool_turn, final_turn]),
        ),
    ):
        await _run_check_loop(ctx, messages, [tool], check_items=[])

    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "c1"
    assert tool_msgs[0].status == expected_status
    assert tool_msgs[0].content == str(result)
