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

"""Unit tests for the Pro session transcript ledger (history redesign §3.2, M2).

Covers the four-region discipline: S-region byte stability across turns, the
append-only active region, the single depth-K scrub edge in the Pro message
shape (UI list / plan recitation strip, ephemeral-block deletion and the
in-place screenshot swap in one pass; grace / placeholder / freeze — the M1
race regressions re-run against HumanMessage observations keyed by step id),
``T+mm:ss`` session-offset timestamps, the tool-call/response pairing
invariant, the cold-start restored-history block, and the silent-turn flag.
"""

import json
import re

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from artemis.memory.step_memory import StepMemoryService
from artemis.memory.transcript import (
    EXECUTION_RESULT_MARKER,
    PLAN_RECITATION_MARKER,
    PRO_UI_LIST_MARKER,
    RESTORED_HISTORY_HEADER,
    TranscriptLedger,
    format_session_offset,
)

OFFSET_RE = re.compile(r"T\+\d{2,}:\d{2}")


def _service() -> StepMemoryService:
    return StepMemoryService(ctx=None)


def _observation(i: int) -> HumanMessage:
    return HumanMessage(
        content=[
            {"type": "text", "text": f"# CURRENT OBSERVATION [T+00:0{i % 10}]"},
            {"type": "text", "text": f"{PLAN_RECITATION_MARKER}\n- [/] milestone {i}"},
            {"type": "text", "text": "--- Current Screenshot ---"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,IMG_{i}"}},
            {"type": "text", "text": f"{PRO_UI_LIST_MARKER}\n[1] button {i}"},
        ]
    )


def _turn(i: int, tool_call_count: int = 1) -> list:
    messages: list = [_observation(i)]
    if tool_call_count:
        tool_calls = [
            {"name": "click", "args": {"target": n + 1}, "id": f"tc{i}-{n}", "type": "tool_call"}
            for n in range(tool_call_count)
        ]
        messages.append(AIMessage(content=f"thinking {i}", tool_calls=tool_calls))
        for tc in tool_calls:
            messages.append(ToolMessage(tool_call_id=tc["id"], content="Action Recorded"))
    return messages


def _play_turn(ledger: TranscriptLedger, i: int, *, prev_key=None, prev_result=None, **turn_kwargs):
    """Mimic the operator's real ordering: commit previous, render, stage."""
    ledger.commit_staged(step_key=prev_key, validator_result=prev_result)
    turn = _turn(i, **turn_kwargs)
    rendered = ledger.render([turn[0]])
    ledger.stage_turn(turn)
    return rendered


def _fingerprint(msg) -> str:
    return json.dumps(msg.content, sort_keys=True, default=str)


def _has_image(msg) -> bool:
    return any(isinstance(b, dict) and b.get("type") in ("image_url", "image") for b in msg.content)


def test_format_session_offset():
    assert format_session_offset(0) == "T+00:00"
    assert format_session_offset(83) == "T+01:23"
    # Minutes never wrap into hours: byte-stable monotonic labels.
    assert format_session_offset(3725) == "T+62:05"
    assert format_session_offset(-5) == "T+00:00"


def test_static_prefix_set_once_and_byte_stable():
    ledger = TranscriptLedger(step_memory=_service())
    system = SystemMessage(content="STATIC SYSTEM PROMPT")
    ledger.set_static_prefix([system])

    with pytest.raises(RuntimeError):
        ledger.set_static_prefix([SystemMessage(content="other")])

    fingerprints = set()
    for i in range(1, 6):
        rendered = _play_turn(ledger, i, prev_key=f"step-{i - 1}" if i > 1 else None)
        assert rendered[0] is system
        fingerprints.add(rendered[0].content)
    assert fingerprints == {"STATIC SYSTEM PROMPT"}


def test_active_region_is_append_only():
    ledger = TranscriptLedger(step_memory=_service())
    seen_ids: list[int] = []
    for i in range(1, 5):
        _play_turn(ledger, i, prev_key=f"step-{i - 1}" if i > 1 else None)
        active = ledger.active_messages
        # The previously observed prefix is unchanged in identity and order.
        assert [id(m) for m in active[: len(seen_ids)]] == seen_ids
        seen_ids = [id(m) for m in active]
    assert ledger.turn_count == 3  # 3 committed, 1 still staged


def test_validator_result_message_carries_session_offset():
    ledger = TranscriptLedger(step_memory=_service())
    _play_turn(ledger, 1)
    _play_turn(
        ledger,
        2,
        prev_key="step-1",
        prev_result={"status": "dispatched", "execution": [{"action": "tap", "attempts": ["ok"]}]},
    )

    result_messages = [
        m
        for m in ledger.active_messages
        if isinstance(m, HumanMessage) and str(m.content).find(EXECUTION_RESULT_MARKER) >= 0
    ]
    assert len(result_messages) == 1
    text = result_messages[0].content[0]["text"]
    assert OFFSET_RE.search(text), text
    assert "Status: dispatched" in text
    assert "ago" not in text


def test_validator_result_skipped_for_actionless_turn():
    ledger = TranscriptLedger(step_memory=_service())
    _play_turn(ledger, 1)
    _play_turn(ledger, 2, prev_key="step-1", prev_result=None)
    assert not any(EXECUTION_RESULT_MARKER in str(m.content) for m in ledger.active_messages)


def test_recitation_and_ui_list_stripped_at_the_text_edge_and_screenshot_at_k():
    """Two edges (text at depth 1, screenshot at K=3): the plan recitation and
    UI list leave as soon as a newer observation exists, the screenshot stays
    until depth K and is then resolved in place; only the live tail ever
    carries an indexed element list."""
    ledger = TranscriptLedger(step_memory=_service(), image_scrub_depth=3)
    _play_turn(ledger, 1)
    rendered = _play_turn(ledger, 2, prev_key="step-1")
    obs1 = ledger.active_messages[0]
    # Turn 1 is now depth 2: text edge passed, screenshot kept.
    assert PLAN_RECITATION_MARKER not in str(obs1.content)
    assert PRO_UI_LIST_MARKER not in str(obs1.content)
    assert _has_image(obs1)
    original_obs1 = _fingerprint(obs1)

    rendered = _play_turn(ledger, 3, prev_key="step-2")
    # Turn 1 reached depth 3: the screenshot is resolved.
    assert not _has_image(obs1)
    assert _fingerprint(obs1) != original_obs1
    # Turn 2 (depth 2): text edge passed, screenshot still there; the live tail keeps both.
    obs2 = next(m for m in ledger.active_messages if "OBSERVATION [T+00:02]" in str(m.content))
    assert PLAN_RECITATION_MARKER not in str(obs2.content)
    assert PRO_UI_LIST_MARKER not in str(obs2.content)
    assert _has_image(obs2)
    tail_text = str(rendered[-1].content)
    assert PLAN_RECITATION_MARKER in tail_text
    assert PRO_UI_LIST_MARKER in tail_text


def test_depth_k_image_resolved_to_ready_visual_summary():
    service = _service()
    service._step_inputs["step-1"] = {"step_number": 1}
    service._summaries["step-1"] = "Objective visual transition 1."

    ledger = TranscriptLedger(step_memory=service, image_scrub_depth=3)
    for i in range(1, 5):
        _play_turn(ledger, i, prev_key=f"step-{i - 1}" if i > 1 else None)

    first_obs = ledger.active_messages[0]
    assert not _has_image(first_obs)
    # (i) The summary sits where the image was, the label above it is gone,
    # recitation and UI list are stripped: exactly two blocks remain.
    assert first_obs.content == [
        {"type": "text", "text": "# CURRENT OBSERVATION [T+00:01]"},
        {
            "type": "text",
            "text": "--- Historical Visual Transition ---\nObjective visual transition 1.",
        },
    ]
    assert "--- Current Screenshot ---" not in str(first_obs.content)


def test_pending_grace_then_placeholder_never_backfilled():
    service = _service()
    service._step_inputs["step-1"] = {"step_number": 1}  # pending job, no summary

    ledger = TranscriptLedger(step_memory=service, image_scrub_depth=2, pending_grace_steps=1)
    # Enough turns to push turn 1 past K + grace.
    for i in range(1, 7):
        _play_turn(ledger, i, prev_key=f"step-{i - 1}" if i > 1 else None)

    first_obs = ledger.active_messages[0]
    assert not _has_image(first_obs)
    assert "[visual summary pending; evidence at DataEngine step 1]" in str(first_obs.content)
    frozen = _fingerprint(first_obs)

    # A late summary must never mutate the frozen message.
    service._summaries["step-1"] = "Too late."
    _play_turn(ledger, 7, prev_key="step-6")
    assert _fingerprint(ledger.active_messages[0]) == frozen


def test_failed_summary_becomes_unavailable_placeholder():
    service = _service()
    service._step_inputs["step-1"] = {"step_number": 1}
    service._failed.add("step-1")

    ledger = TranscriptLedger(step_memory=service, image_scrub_depth=2, pending_grace_steps=5)
    for i in range(1, 4):
        _play_turn(ledger, i, prev_key=f"step-{i - 1}" if i > 1 else None)

    first_obs = ledger.active_messages[0]
    assert not _has_image(first_obs)
    assert "[visual summary unavailable; evidence at DataEngine step 1]" in str(first_obs.content)


def test_tool_call_response_pairs_are_never_split():
    ledger = TranscriptLedger(step_memory=_service(), image_scrub_depth=2)
    for i in range(1, 7):
        _play_turn(
            ledger,
            i,
            prev_key=f"step-{i - 1}" if i > 1 else None,
            prev_result={"status": "dispatched"} if i > 1 else None,
            tool_call_count=2,
        )

    active = list(ledger.active_messages)
    for idx, msg in enumerate(active):
        if isinstance(msg, AIMessage) and msg.tool_calls:
            following = active[idx + 1 : idx + 1 + len(msg.tool_calls)]
            assert [getattr(m, "tool_call_id", None) for m in following] == [
                tc["id"] for tc in msg.tool_calls
            ], f"tool-call pairing split at active index {idx}"


def test_restored_history_only_seeds_an_empty_ledger():
    ledger = TranscriptLedger(step_memory=_service())
    ledger.set_restored_history(f"{RESTORED_HISTORY_HEADER} steps 1-9 ...")
    assert ledger.has_restored_history

    with pytest.raises(RuntimeError):
        ledger.set_restored_history("again")

    ledger.set_static_prefix([SystemMessage(content="S")])
    rendered = _play_turn(ledger, 1)
    # Order: S region, then the frozen restored block, then the live tail.
    assert rendered[0].content == "S"
    assert RESTORED_HISTORY_HEADER in str(rendered[1].content)
    assert "# CURRENT OBSERVATION" in str(rendered[-1].content)

    ledger2 = TranscriptLedger(step_memory=_service())
    _play_turn(ledger2, 1)
    ledger2.commit_staged(step_key="step-1")
    with pytest.raises(RuntimeError):
        ledger2.set_restored_history("late")


def test_stage_twice_commits_the_forgotten_turn():
    ledger = TranscriptLedger(step_memory=_service())
    ledger.stage_turn(_turn(1))
    ledger.stage_turn(_turn(2))
    assert ledger.turn_count == 1
    assert ledger.has_staged_turn


def test_no_ago_wording_in_ledger_output():
    ledger = TranscriptLedger(step_memory=_service())
    for i in range(1, 4):
        _play_turn(
            ledger,
            i,
            prev_key=f"step-{i - 1}" if i > 1 else None,
            prev_result={"status": "dispatched"} if i > 1 else None,
        )
    blob = " ".join(str(m.content) for m in ledger.active_messages)
    assert " ago" not in blob


# ---------------------------------------------------------------------------
# Session-start anchoring and multi-step turns (Flash profile reuse)
# ---------------------------------------------------------------------------


def test_session_start_anchors_offsets_to_the_data_engine_clock():
    """With a ``session_start`` epoch the ledger reads the wall clock, so the
    ``T+mm:ss`` labels are relative to the recorded session start (the same
    origin as the chunk ledger lines and the video analyzer's timeline)."""
    now = {"t": 1000.0}
    ledger = TranscriptLedger(step_memory=_service(), clock=lambda: now["t"], session_start=940.0)
    assert ledger.elapsed_label() == "T+01:00"
    now["t"] = 1075.5
    assert ledger.elapsed_label() == "T+02:15"


def test_without_session_start_the_ledger_keeps_its_own_origin():
    now = {"t": 500.0}
    ledger = TranscriptLedger(step_memory=_service(), clock=lambda: now["t"])
    assert ledger.elapsed_label() == "T+00:00"
    now["t"] = 512.0
    assert ledger.elapsed_label() == "T+00:12"


def test_commit_records_every_step_key_of_a_multi_action_turn():
    ledger = TranscriptLedger(step_memory=_service())
    ledger.stage_turn(_turn(1, tool_call_count=2))
    ledger.commit_staged(step_key="s1", extra_step_keys=["s2", "s1", "s3"])
    turn = ledger.unchunked_turns()[0]
    assert turn["step_key"] == "s1"
    assert turn["step_keys"] == ["s1", "s2", "s3"]

    ledger.stage_turn(_turn(2))
    ledger.commit_staged(step_key=None)
    assert ledger.unchunked_turns()[1]["step_keys"] == []


# ---------------------------------------------------------------------------
# Turn transcript rendering (chunk-capsule source)
# ---------------------------------------------------------------------------


def test_render_turn_transcript_is_lossless_over_text_thinking_tools_and_results():
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    from artemis.memory.transcript import SCREENSHOT_PLACEHOLDER, render_turn_transcript

    messages = [
        HumanMessage(
            content=[
                {"type": "text", "text": "# CURRENT OBSERVATION [T+01:00]"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,IMG"}},
            ]
        ),
        AIMessage(
            content=[
                {"type": "thinking", "thinking": "compare the two prices"},
                {"type": "text", "text": "I will recall the earlier price."},
            ],
            tool_calls=[
                {
                    "name": "search_history",
                    "args": {"query": "price"},
                    "id": "c1",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(tool_call_id="c1", content="Step 3: price ¥39", status="success"),
        ToolMessage(
            tool_call_id="c2", name="adb_shell", content="permission denied", status="error"
        ),
        ToolMessage(tool_call_id="c9", content="orphan result"),
        SystemMessage(content="You have reached the maximum number of tool calls"),
        AIMessage(
            content="tap now",
            tool_calls=[{"name": "click", "args": {"target": [0.5, 0.5]}, "id": "c3"}],
        ),
    ]
    text = render_turn_transcript(messages)
    assert text.splitlines()[0] == "[observation]"
    assert "# CURRENT OBSERVATION [T+01:00]" in text
    assert SCREENSHOT_PLACEHOLDER in text and "IMG" not in text
    assert "[operator]\n(thinking) compare the two prices\nI will recall the earlier price." in text
    assert '[tool call] search_history({"query": "price"})' in text
    # A Pro ToolMessage carries no name: resolved through the turn's own call.
    assert "[tool result search_history]\nStep 3: price ¥39" in text
    assert "[tool result adb_shell (error)]\npermission denied" in text
    assert "[tool result c9]\norphan result" in text  # unresolvable: id shown
    assert "[system]\nYou have reached the maximum number of tool calls" in text
    assert '[tool call] click({"target": [0.5, 0.5]})' in text
    # Order is message order.
    assert text.index("search_history(") < text.index("price ¥39") < text.index("tap now")


def test_ledger_turn_transcript_reflects_the_scrubbed_active_region():
    """The transcript handed to chunk compression is the turn as it stands
    after the scrub edge: the UI list stripped, the validator result present."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from artemis.memory.transcript import PRO_UI_LIST_MARKER, TranscriptLedger

    ledger = TranscriptLedger(clock=lambda: 0.0)

    def turn(i: int) -> list:
        return [
            HumanMessage(
                content=[
                    {"type": "text", "text": f"# CURRENT OBSERVATION [T+00:0{i}]"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,IMG{i}"}},
                    {"type": "text", "text": f"{PRO_UI_LIST_MARKER}\n[0] Button {i}"},
                ]
            ),
            AIMessage(
                content=f"thought {i}",
                tool_calls=[{"name": "click", "args": {}, "id": f"t{i}"}],
            ),
            ToolMessage(tool_call_id=f"t{i}", content="Action Recorded"),
        ]

    ledger.stage_turn(turn(1))
    ledger.commit_staged(step_key="s1", validator_result={"status": "dispatched"})
    ledger.stage_turn(turn(2))
    ledger.commit_staged(step_key="s2", validator_result={"status": "failed", "error": "boom"})
    ledger.render([HumanMessage(content=[{"type": "text", "text": "tail"}])])

    first, second = ledger.unchunked_turns()
    text = ledger.turn_transcript(first)
    assert "thought 1" in text and "[tool call] click({})" in text
    assert "Button 1" not in text  # depth-1 strip already applied
    assert "--- Action Execution Result (T+00:00) ---\nStatus: dispatched" in text
    assert "Status: failed" in ledger.turn_transcript(second)
    assert "thought 2" not in text  # a turn transcript never leaks into another turn


# ---------------------------------------------------------------------------
# Occupancy-driven screenshot depth
# ---------------------------------------------------------------------------


def _image_turns(ledger) -> list[int]:
    """1-based turn numbers whose observation still carries its screenshot."""
    return [
        i + 1
        for i, msg in enumerate(
            m for m in ledger.active_messages if "OBSERVATION" in str(m.content)
        )
        if _has_image(msg)
    ]


def _banded_ledger(**kwargs) -> TranscriptLedger:
    return TranscriptLedger(
        step_memory=_service(),
        image_scrub_depth=3,
        image_scrub_depth_relaxed=6,
        context_budget_tokens=100_000,
        start_ratio=0.35,
        **kwargs,
    )


def test_relaxed_depth_below_start_gate_keeps_more_screenshots():
    """Below the start gate the scrub edge sits at the relaxed depth. Depth K
    scrubs the K-th most recent screenshot with the live tail at depth 1, so
    K=6 keeps four historical screenshots in view (K=3 keeps one). An unknown
    occupancy counts as below the gate."""
    ledger = _banded_ledger()
    assert ledger.occupancy is None
    assert ledger.effective_image_scrub_depth == 6
    for i in range(1, 10):
        _play_turn(ledger, i, prev_key=f"step-{i - 1}" if i > 1 else None)
    # At the render of turn 9 its observation is the live tail (depth 1);
    # committed turns 8..5 are depths 2..5 and turn 4 (depth 6) is scrubbed.
    ledger.record_prompt_tokens(20_000)  # 20% < 35%
    assert ledger.effective_image_scrub_depth == 6
    assert _image_turns(ledger) == [5, 6, 7, 8]

    tight = TranscriptLedger(step_memory=_service(), image_scrub_depth=3)
    for i in range(1, 10):
        _play_turn(tight, i, prev_key=f"step-{i - 1}" if i > 1 else None)
    assert _image_turns(tight) == [8]


def test_crossing_the_start_gate_tightens_once_and_relaxing_never_backfills():
    ledger = _banded_ledger()
    for i in range(1, 10):
        _play_turn(ledger, i, prev_key=f"step-{i - 1}" if i > 1 else None)
    assert _image_turns(ledger) == [5, 6, 7, 8]

    # Past the gate: the next render scrubs the extra depths in one pass
    # (turns 5–8 were depths 3–6 at that render; only turn 9 at depth 2 stays).
    ledger.record_prompt_tokens(40_000)  # 40% >= 35%
    assert ledger.effective_image_scrub_depth == 3
    _play_turn(ledger, 10, prev_key="step-9")
    assert _image_turns(ledger) == [9]

    # Back under the gate: nothing is restored; new turns accumulate again
    # until four historical screenshots are in view.
    ledger.record_prompt_tokens(20_000)
    assert ledger.effective_image_scrub_depth == 6
    _play_turn(ledger, 11, prev_key="step-10")
    assert _image_turns(ledger) == [9, 10]
    _play_turn(ledger, 12, prev_key="step-11")
    assert _image_turns(ledger) == [9, 10, 11]
    _play_turn(ledger, 13, prev_key="step-12")
    assert _image_turns(ledger) == [9, 10, 11, 12]
    _play_turn(ledger, 14, prev_key="step-13")  # turn 9 reaches depth 6
    assert _image_turns(ledger) == [10, 11, 12, 13]


def test_no_band_configuration_keeps_the_fixed_depth():
    ledger = TranscriptLedger(step_memory=_service(), image_scrub_depth=3)
    ledger.record_prompt_tokens(1)
    assert ledger.effective_image_scrub_depth == 3
    relaxed_only = TranscriptLedger(
        step_memory=_service(), image_scrub_depth=3, image_scrub_depth_relaxed=6
    )
    assert relaxed_only.effective_image_scrub_depth == 3  # no gate → never relaxed


def test_scrub_edge_runs_before_the_chunker_at_render():
    """The chunker sees the active window after the scrub edge advanced, so a
    chunk closing over turns older than the floor never captures a live
    screenshot in its source transcript."""
    ledger = _banded_ledger()
    seen: list[list[int]] = []

    class Probe:
        def on_render(self, led):
            seen.append(_image_turns(led))

    ledger.attach_chunker(Probe())
    for i in range(1, 9):
        _play_turn(ledger, i, prev_key=f"step-{i - 1}" if i > 1 else None)
    # At the render of turn 8 the scrub edge (depth 6) had already resolved
    # turn 3 (depth 6); the probe saw turns 4–7 with images, never turn 3.
    assert seen[-1] == [4, 5, 6, 7]


# ---------------------------------------------------------------------------
# Ephemeral blocks and stable repeated renders
# ---------------------------------------------------------------------------


def _ready_service(n: int) -> StepMemoryService:
    service = _service()
    for i in range(1, n + 1):
        service._step_inputs[f"step-{i}"] = {"step_number": i}
        service._summaries[f"step-{i}"] = f"Visual transition {i}."
    return service


def test_ephemeral_blocks_vanish_at_the_text_edge_and_not_before():
    """(ii) Per-turn notices marked through ``mark_ephemeral`` stay only while
    the observation is the live tail; they are deleted at the text edge
    together with the strip, before the screenshot is resolved at K."""
    from artemis.memory.transcript import EPHEMERAL_BLOCKS_KEY, mark_ephemeral

    ledger = TranscriptLedger(step_memory=_ready_service(6), image_scrub_depth=3)

    def turn_with_notice(i: int) -> list:
        turn = _turn(i)
        obs = turn[0]
        obs.content = [
            obs.content[0],
            {"type": "text", "text": f"[Reminder for turn {i}: state your reasoning]"},
            *obs.content[1:],
            {"type": "text", "text": f"[User guidance for turn {i}]"},
        ]
        mark_ephemeral(obs, [1, len(obs.content) - 1])
        return turn

    ledger.commit_staged()
    ledger.render([turn_with_notice(1)[0]])
    ledger.stage_turn(turn_with_notice(1))
    for i in range(2, 4):
        ledger.commit_staged(step_key=f"step-{i - 1}")
        ledger.render([_observation(i)])
        ledger.stage_turn(turn_with_notice(i))

    obs1 = ledger.active_messages[0]
    # At the render of turn 3, turn 1 is depth 3 (K): rewritten and frozen.
    assert obs1.content == [
        {"type": "text", "text": "# CURRENT OBSERVATION [T+00:01]"},
        {"type": "text", "text": "--- Historical Visual Transition ---\nVisual transition 1."},
    ]
    assert EPHEMERAL_BLOCKS_KEY not in obs1.additional_kwargs
    # Turn 2 is depth 2: past the text edge, its notices are gone while the
    # screenshot is still there; the consumed indices are cleared.
    obs2 = next(m for m in ledger.active_messages if "OBSERVATION [T+00:02]" in str(m.content))
    assert "[Reminder for turn 2: state your reasoning]" not in str(obs2.content)
    assert "[User guidance for turn 2]" not in str(obs2.content)
    assert _has_image(obs2)
    assert EPHEMERAL_BLOCKS_KEY not in obs2.additional_kwargs


def test_rendering_past_the_edge_is_byte_identical_and_prefix_stable():
    """(iii) A render with nothing new is byte-identical to the previous one,
    and across turns exactly one message — the observation reaching depth K —
    differs from the previous render's prefix, so the request prefix before
    it is what the provider's prompt cache already holds."""
    ledger = TranscriptLedger(step_memory=_ready_service(12), image_scrub_depth=3)
    ledger.set_static_prefix([SystemMessage(content="STATIC")])

    def snapshot(rendered: list) -> list[str]:
        return [_fingerprint(m) for m in rendered[:-1]]  # everything but the live tail

    previous: list[str] = []
    for i in range(1, 11):
        rendered = _play_turn(
            ledger,
            i,
            prev_key=f"step-{i - 1}" if i > 1 else None,
            prev_result={"status": "dispatched"} if i > 1 else None,
        )
        current = snapshot(rendered)
        changed = [idx for idx, fp in enumerate(previous) if current[idx] != fp]
        if i >= 3:
            # The live tail is depth 1 and committed turn i-1 depth 2, so the
            # turn reaching depth 3 is i-2; its observation heads its span.
            expected = next(
                idx
                for idx, m in enumerate(rendered[:-1])
                if f"OBSERVATION [T+00:0{(i - 2) % 10}]" in str(m.content)
            )
            assert changed == [expected], (i, changed)
        else:
            assert changed == [], (i, changed)
        previous = current

    # Same state, rendered again: byte-identical, including the tail.
    tail = [_observation(11)]
    again = [_fingerprint(m) for m in ledger.render(tail)]
    assert again == [_fingerprint(m) for m in ledger.render(tail)]
    assert again[:-1] == previous


# ---------------------------------------------------------------------------
# Silent-turn flag
# ---------------------------------------------------------------------------


def _commit(messages: list) -> TranscriptLedger:
    ledger = TranscriptLedger(step_memory=_service())
    ledger.stage_turn(messages)
    ledger.commit_staged(step_key="s1")
    return ledger


def test_last_turn_silent_when_ai_messages_show_no_text():
    click = [{"name": "click", "args": {"target": 1}, "id": "c1", "type": "tool_call"}]
    assert _commit([_observation(1), AIMessage(content="", tool_calls=click)]).last_turn_silent
    assert _commit([_observation(1), AIMessage(content="   \n", tool_calls=click)]).last_turn_silent
    assert _commit(
        [
            _observation(1),
            AIMessage(content=[{"type": "thinking", "thinking": "hidden"}], tool_calls=click),
        ]
    ).last_turn_silent
    assert _commit(
        [
            _observation(1),
            AIMessage(content=[{"type": "text", "text": "  "}], tool_calls=click),
            ToolMessage(tool_call_id="c1", content="Action Recorded"),
            AIMessage(content=[], tool_calls=click),
        ]
    ).last_turn_silent


def test_last_turn_not_silent_with_visible_text_or_without_ai_message():
    click = [{"name": "click", "args": {"target": 1}, "id": "c1", "type": "tool_call"}]
    assert not _commit(
        [_observation(1), AIMessage(content="I tap it.", tool_calls=click)]
    ).last_turn_silent
    assert not _commit(
        [
            _observation(1),
            AIMessage(
                content=[{"type": "thinking", "thinking": "x"}, {"type": "text", "text": "Tap."}],
                tool_calls=click,
            ),
        ]
    ).last_turn_silent
    # One silent call followed by a spoken one: the turn spoke.
    assert not _commit(
        [
            _observation(1),
            AIMessage(content="", tool_calls=click),
            ToolMessage(tool_call_id="c1", content="ok"),
            AIMessage(content="Now the real move.", tool_calls=click),
        ]
    ).last_turn_silent
    # No AI message at all is not a silent turn.
    assert not _commit([_observation(1)]).last_turn_silent
    # A tool result with empty text does not count as an AI message.
    assert not _commit(
        [_observation(1), ToolMessage(tool_call_id="c1", content="")]
    ).last_turn_silent


def test_last_turn_silent_tracks_each_commit():
    ledger = TranscriptLedger(step_memory=_service())
    click = [{"name": "click", "args": {}, "id": "c1", "type": "tool_call"}]
    assert ledger.last_turn_silent is False
    ledger.stage_turn([_observation(1), AIMessage(content="", tool_calls=click)])
    ledger.commit_staged(step_key="s1")
    assert ledger.last_turn_silent is True
    ledger.stage_turn([_observation(2), AIMessage(content="spoken", tool_calls=click)])
    ledger.commit_staged(step_key="s2")
    assert ledger.last_turn_silent is False


def test_last_turn_silent_recognises_streamed_ai_message_chunks():
    """Streamed replies are AIMessageChunk (type "AIMessageChunk"), not "ai"."""
    from langchain_core.messages import AIMessageChunk, HumanMessage

    from artemis.memory.transcript import TranscriptLedger, render_turn_transcript

    ledger = TranscriptLedger()
    ledger.stage_turn([HumanMessage(content="obs"), AIMessageChunk(content=[])])
    ledger.commit_staged(step_key=None, validator_result=None)
    assert ledger.last_turn_silent is True

    ledger.stage_turn([HumanMessage(content="obs"), AIMessageChunk(content="I see the list.")])
    ledger.commit_staged(step_key=None, validator_result=None)
    assert ledger.last_turn_silent is False

    rendered = render_turn_transcript([AIMessageChunk(content="I see the list.")])
    assert rendered.startswith("[operator]")
