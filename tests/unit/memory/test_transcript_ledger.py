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
append-only active region, depth-1 stripping of old UI lists and plan
recitations, the depth-K screenshot scrub in the Pro message shape (grace /
placeholder / freeze — the M1 race regressions re-run against HumanMessage
observations keyed by step id), ``T+mm:ss`` session-offset timestamps, the
tool-call/response pairing invariant, and the cold-start restored-history
block.
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
    return any(
        isinstance(b, dict) and b.get("type") in ("image_url", "image") for b in msg.content
    )


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
        prev_result={"status": "success", "execution": [{"action": "tap", "attempts": ["ok"]}]},
    )

    result_messages = [
        m
        for m in ledger.active_messages
        if isinstance(m, HumanMessage)
        and str(m.content).find(EXECUTION_RESULT_MARKER) >= 0
    ]
    assert len(result_messages) == 1
    text = result_messages[0].content[0]["text"]
    assert OFFSET_RE.search(text), text
    assert "Status: success" in text
    assert "ago" not in text


def test_validator_result_skipped_for_actionless_turn():
    ledger = TranscriptLedger(step_memory=_service())
    _play_turn(ledger, 1)
    _play_turn(ledger, 2, prev_key="step-1", prev_result=None)
    assert not any(
        EXECUTION_RESULT_MARKER in str(m.content) for m in ledger.active_messages
    )


def test_old_turn_recitation_and_ui_list_stripped_at_depth_1():
    ledger = TranscriptLedger(step_memory=_service())
    _play_turn(ledger, 1)
    rendered = _play_turn(ledger, 2, prev_key="step-1")

    committed_obs = ledger.active_messages[0]
    committed_text = str(committed_obs.content)
    # Depth-1 scrub: the previous turn's plan recitation and UI list are gone.
    assert PLAN_RECITATION_MARKER not in committed_text
    assert PRO_UI_LIST_MARKER not in committed_text
    # Its screenshot is still inside the K-depth active window.
    assert _has_image(committed_obs)
    # The live tail keeps both.
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
    assert {
        "type": "text",
        "text": "--- Historical Visual Transition ---\nObjective visual transition 1.",
    } in first_obs.content


def test_pending_grace_then_placeholder_never_backfilled():
    service = _service()
    service._step_inputs["step-1"] = {"step_number": 1}  # pending job, no summary

    ledger = TranscriptLedger(
        step_memory=service, image_scrub_depth=2, pending_grace_steps=1
    )
    # Enough turns to push turn 1 past K + grace.
    for i in range(1, 7):
        _play_turn(ledger, i, prev_key=f"step-{i - 1}" if i > 1 else None)

    first_obs = ledger.active_messages[0]
    assert not _has_image(first_obs)
    assert "[visual summary pending; evidence at DataEngine step 1]" in str(
        first_obs.content
    )
    frozen = _fingerprint(first_obs)

    # A late summary must never mutate the frozen message.
    service._summaries["step-1"] = "Too late."
    _play_turn(ledger, 7, prev_key="step-6")
    assert _fingerprint(ledger.active_messages[0]) == frozen


def test_failed_summary_becomes_unavailable_placeholder():
    service = _service()
    service._step_inputs["step-1"] = {"step_number": 1}
    service._failed.add("step-1")

    ledger = TranscriptLedger(
        step_memory=service, image_scrub_depth=2, pending_grace_steps=5
    )
    for i in range(1, 4):
        _play_turn(ledger, i, prev_key=f"step-{i - 1}" if i > 1 else None)

    first_obs = ledger.active_messages[0]
    assert not _has_image(first_obs)
    assert "[visual summary unavailable; evidence at DataEngine step 1]" in str(
        first_obs.content
    )


def test_tool_call_response_pairs_are_never_split():
    ledger = TranscriptLedger(step_memory=_service(), image_scrub_depth=2)
    for i in range(1, 7):
        _play_turn(
            ledger,
            i,
            prev_key=f"step-{i - 1}" if i > 1 else None,
            prev_result={"status": "success"} if i > 1 else None,
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
            prev_result={"status": "success"} if i > 1 else None,
        )
    blob = " ".join(str(m.content) for m in ledger.active_messages)
    assert " ago" not in blob
