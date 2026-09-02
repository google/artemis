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

"""L2/L3 history chunk compression (history redesign §3.3, M3).

Covers: the mechanical band-③ action ledger (per-step lines, FA recovery
sub-lines, verbatim never-evict user injections), the band-② interval coverage
machine check, the three-band chunk block structure, all triggers (real
milestone switch via stamped-hash change, pseudo-switch rollback protection,
segment size, fake-meter soft/hard thresholds), the sliding-window floor, the
tool-pair invariant at the F/A boundary, frozen-block byte stability between
compression events, checkpoint post-hoc annotation version bumps, era merging,
the extreme-layer recall-only period paragraph (step range + start/end
offsets + mechanically assembled synopsis, §10 decision 5 final review), and
step addressability (step number + ``T+mm:ss``) after every compression level
— degrading to period addressability at the extreme layer only.
"""

import json
import re
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from artemis.memory.chunking import (
    CHUNK_PENDING_NOTE,
    ChunkState,
    EraState,
    HistoryChunkManager,
    StepCapsuleLens,
    build_action_ledger,
    render_era_block,
    render_era_period_paragraph,
    validate_interval_coverage,
)
from artemis.memory.step_memory import StepMemoryService
from artemis.memory.transcript import TranscriptLedger

SESSION_START = 1000.0


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _step(i: int, subgoal: str = "hash-a", **overrides) -> dict:
    step = {
        "step_id": f"s{i}",
        "step_number": i,
        "timestamp": SESSION_START + 10.0 * i,
        "summary": f"Visual transition of step {i}.",
        "action_taken": {"action": "tap", "target_text": f"btn{i}", "coordinates": [i, i]},
        "last_execution_result": {"status": "success"},
        "operator_raw_thinking": f"thinking {i}",
        "interleaved_events": [],
        "extra_metadata": {"subgoal_hash": subgoal},
    }
    step.update(overrides)
    return step


class FakeEngine:
    def __init__(self, steps: list[dict], base_dir=None):
        self.steps = steps
        self.session_start_time = SESSION_START
        self.current_session_id = "session-1"
        self.chunk_writes: list[dict] = []
        if base_dir is not None:
            self.base_dir = base_dir

    def get_agent_friendly_steps(self):
        return self.steps

    def record_history_chunk(self, **kwargs):
        self.chunk_writes.append(kwargs)
        return f"chunk-row-{len(self.chunk_writes)}"


class StubCapsuleService(StepMemoryService):
    """Records capsule submissions without spawning asyncio tasks (sync tests).

    ``auto_resolve=True`` makes every submitted capsule ready immediately, so
    the ready-gated swap fires at the same render that closed the segment —
    the shape trigger/era/L3 tests need. The default (manual) form keeps
    capsules pending until ``resolve`` is called, which is the gating tests'
    lever.
    """

    def __init__(self, auto_resolve: bool = False):
        super().__init__(ctx=None)
        self.submitted: list[str] = []
        self.auto_resolve = auto_resolve

    def submit(self, key, payload, aliases=()):
        self.submitted.append(str(key))
        self._step_inputs[key] = payload
        self._failed.discard(key)
        if self.auto_resolve:
            self.resolve(key, _capsule(payload["start_step"], payload["end_step"]))

    def resolve(self, key, capsule: dict):
        self._summaries[key] = json.dumps(capsule, ensure_ascii=False)


def _capsule(start: int, end: int) -> dict:
    return {
        "doing": "Working toward the login milestone.",
        "did": "Walked the SMS-code login path",
        "effect": "Logged-in state; note notes/login_flow.md records the code entry path.",
        "entry_state": "Home screen, logged out",
        "exit_state": "Settings page open",
        "verified_facts": [f"fact-{start}"],
        "unresolved": ["u1"],
        "failed_paths": [],
        "important_entities": ["account A"],
        "intervals": [
            {"start_step": start, "end_step": end, "text": "carried out the segment's taps"}
        ],
    }


def _observation(i: int) -> HumanMessage:
    return HumanMessage(
        content=[
            {"type": "text", "text": f"# CURRENT OBSERVATION [T+00:{i % 60:02d}]"},
            {"type": "text", "text": "--- Current Screenshot ---"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,IMG_{i}"}},
        ]
    )


def _turn(i: int) -> list:
    tool_call = {"name": "click", "args": {"target": i}, "id": f"tc{i}", "type": "tool_call"}
    return [
        _observation(i),
        AIMessage(content=f"thinking {i}", tool_calls=[tool_call]),
        ToolMessage(tool_call_id=f"tc{i}", content="Action Recorded"),
    ]


def _make(
    steps: list[dict],
    *,
    min_active: int = 2,
    max_steps: int = 12,
    target_tokens: int = 10**9,
    max_chunks: int = 8,
    max_eras: int | None = None,
    meter=None,
    auto_capsule: bool = False,
):
    ledger = TranscriptLedger(step_memory=StepMemoryService(ctx=None))
    engine = FakeEngine(steps)
    capsule = StubCapsuleService(auto_resolve=auto_capsule)
    chunker = HistoryChunkManager(
        engine=engine,
        chunking_config=SimpleNamespace(
            max_steps=max_steps,
            target_source_tokens=target_tokens,
            model="test-model",
            max_chunks=max_chunks,
            max_eras=max_eras,
        ),
        transcript_config=SimpleNamespace(
            context_budget_tokens=100_000,
            soft_ratio=0.7,
            hard_ratio=0.9,
            min_active_steps=min_active,
        ),
        capsule_service=capsule,
        meter_getter=meter or (lambda: None),
        goal="TEST GOAL",
    )
    ledger.attach_chunker(chunker)
    return ledger, chunker, engine, capsule


def _run_turns(ledger, chunker, start: int, upto: int, hash_for=None):
    """Mimic the real per-turn ordering: commit previous → render (chunker
    hook) → stage this turn → stamp this turn's step (execution_check)."""
    hash_for = hash_for or (lambda i: "hash-a")
    rendered = None
    for i in range(start, upto + 1):
        ledger.commit_staged(
            step_key=f"s{i - 1}" if i > 1 else None,
            validator_result={"status": "success"} if i > 1 else None,
        )
        rendered = ledger.render([_observation(i)])
        ledger.stage_turn(_turn(i))
        chunker.on_step_stamped(f"s{i}", hash_for(i))
    return rendered


def _rendered_text(messages) -> str:
    return "\n".join(str(getattr(m, "content", "")) for m in messages)


def _frozen_text(ledger) -> str:
    return "\n".join(str(m.content) for m in ledger.frozen_blocks)


# ---------------------------------------------------------------------------
# Band ③ — mechanical action ledger
# ---------------------------------------------------------------------------


def test_action_ledger_lines_carry_step_number_offset_action_and_result():
    steps = [_step(1), _step(2, last_execution_result={"status": "failed", "error": "boom"})]
    ledger_text = build_action_ledger(steps, SESSION_START)
    lines = ledger_text.splitlines()
    assert lines[0] == "- Step 1 (T+00:10): Tapped 'btn1' at [1, 1] -> executed"
    assert lines[1].startswith("- Step 2 (T+00:20): Tapped 'btn2' at [2, 2] -> Error: boom")


def test_action_ledger_renders_fa_recovery_sublines_indented():
    steps = [
        _step(
            3,
            interleaved_events=[
                {
                    "type": "tool_call",
                    "name": "_exec_click",
                    "args": {"target": [5, 6], "target_text": "Retry"},
                    "result": "ok",
                },
                {"type": "tool_call", "name": "report_failure_analysis", "args": {}, "result": ""},
            ],
        )
    ]
    text = build_action_ledger(steps, SESSION_START)
    lines = text.splitlines()
    assert lines[0].startswith("- Step 3 ")
    assert lines[1].startswith("    FA: Tapped 'Retry'")
    assert "report_failure_analysis" not in text


def test_action_ledger_preserves_injected_instruction_verbatim():
    steps = [
        _step(4, extra_metadata={"subgoal_hash": "h", "injected_instruction": "跳过弹窗，直接登录"})
    ]
    full = build_action_ledger(steps, SESSION_START)
    minimal = build_action_ledger(steps, SESSION_START, minimal=True)
    expected = '  User @ Step 4: "跳过弹窗，直接登录"'
    assert expected in full
    assert expected in minimal  # never-evict at every width


def test_action_ledger_minimal_width_keeps_step_and_time_addressability():
    steps = [_step(i) for i in range(1, 4)]
    minimal = build_action_ledger(steps, SESSION_START, minimal=True)
    for i in range(1, 4):
        assert f"- Step {i} (T+00:{10 * i:02d}): Tapped 'btn{i}'" in minimal
    assert "-> executed" not in minimal


# ---------------------------------------------------------------------------
# Band ② — machine-checked coverage + capsule parsing
# ---------------------------------------------------------------------------


def test_interval_coverage_requires_seamless_union():
    ok = [{"start_step": 3, "end_step": 5, "text": "a"}, {"start_step": 6, "end_step": 6, "text": "b"}]
    assert validate_interval_coverage(ok, 3, 6)
    gap = [{"start_step": 3, "end_step": 4, "text": "a"}, {"start_step": 6, "end_step": 6, "text": "b"}]
    assert not validate_interval_coverage(gap, 3, 6)
    overlap = [{"start_step": 3, "end_step": 5, "text": "a"}, {"start_step": 5, "end_step": 6, "text": "b"}]
    assert not validate_interval_coverage(overlap, 3, 6)
    short = [{"start_step": 3, "end_step": 5, "text": "a"}]
    assert not validate_interval_coverage(short, 3, 6)
    assert not validate_interval_coverage([], 3, 6)


def test_capsule_parse_rejects_coverage_gap_and_accepts_full_cover():
    lens = StepCapsuleLens(model_name="test", llm=object())
    payload = {"start_step": 3, "end_step": 6}
    good = json.dumps(_capsule(3, 6))
    parsed = lens.parse_capsule(good, payload)
    assert parsed is not None
    assert parsed["verified_facts"] == ["fact-3"]

    bad = _capsule(3, 6)
    bad["intervals"] = [{"start_step": 3, "end_step": 4, "text": "partial"}]
    assert lens.parse_capsule(json.dumps(bad), payload) is None
    assert lens.parse_capsule("not json", payload) is None


@pytest.mark.asyncio
async def test_capsule_lens_render_returns_none_on_gap_so_service_retries():
    class GapLLM:
        async def ainvoke(self, messages):
            capsule = _capsule(1, 2)
            capsule["intervals"] = [{"start_step": 1, "end_step": 1, "text": "only one"}]
            return SimpleNamespace(content=json.dumps(capsule))

    lens = StepCapsuleLens(model_name="test", llm=GapLLM())
    payload = {"start_step": 1, "end_step": 2, "steps": []}
    assert await lens.render("chunk:1-2", payload) is None

    class FullLLM:
        async def ainvoke(self, messages):
            return SimpleNamespace(content=json.dumps(_capsule(1, 2)))

    lens_ok = StepCapsuleLens(model_name="test", llm=FullLLM())
    result = await lens_ok.render("chunk:1-2", payload)
    assert result is not None and json.loads(result)["doing"]


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


def _hashes(boundary_after: int):
    return lambda i: "hash-a" if i <= boundary_after else "hash-b"


def test_milestone_switch_closes_previous_segment_whole_and_gates_swap():
    """Ready-gated swap (user decision 2026-09-01): a milestone switch closes
    the previous segment whole and dispatches its capsule, but the original
    turns stay in the transcript until the capsule header is ready; the swap
    happens at the first render after readiness."""
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 9)]
    ledger, chunker, engine, capsule = _make(steps, min_active=2)
    rendered = _run_turns(ledger, chunker, 1, 8, _hashes(4))

    # Closed whole as ONE awaiting chunk; NOT swapped (capsule pending).
    assert len(chunker.chunks) == 0
    assert len(chunker.awaiting_chunks) == 1
    chunk = chunker.awaiting_chunks[0]
    assert (chunk.start_step_number, chunk.end_step_number) == (1, 4)
    assert chunk.subgoal_hash == "hash-a"
    assert chunk.status == "pending"

    # v1 pending row persisted with band ③ (recall/auxiliary views see it).
    assert engine.chunk_writes[0]["version"] == 1
    assert engine.chunk_writes[0]["status"] == "pending"
    assert "- Step 1 (T+00:10)" in engine.chunk_writes[0]["band3"]

    # Capsule dispatched in the background.
    assert capsule.submitted == ["chunk:1-4"]

    # Original history retained verbatim: no chunk block, no frozen region,
    # turn 1's observation still live in the rendered transcript.
    text = _rendered_text(rendered)
    assert "[Chunk 1" not in text
    assert ledger.frozen_blocks == ()
    assert "# CURRENT OBSERVATION [T+00:01]" in text

    # Capsule ready → the next render swaps: frozen ①②③ block, originals out.
    capsule.resolve("chunk:1-4", _capsule(1, 4))
    ledger.commit_staged(step_key="s8", validator_result={"status": "success"})
    rendered_after = ledger.render([_observation(9)])
    assert len(chunker.chunks) == 1
    assert chunker.awaiting_chunks == ()
    frozen = _frozen_text(ledger)
    assert "[Chunk 1 | Steps 1–4" in frozen
    assert "① Synopsis & effects" in frozen
    assert "③ Step action ledger" in frozen
    assert CHUNK_PENDING_NOTE not in frozen
    assert "# CURRENT OBSERVATION [T+00:01]" not in _rendered_text(rendered_after)
    # The ready write bumped the DB version (append-only).
    ready_rows = [w for w in engine.chunk_writes if w["status"] == "ready"]
    assert ready_rows and ready_rows[0]["version"] == 2


def test_plan_write_hint_without_stamp_change_is_discarded():
    """Pseudo-switch protection: a vetoed+rolled-back plan write queues a hint
    that the next stamped step (same hash) discards — no chunk is created."""
    steps = [_step(i) for i in range(1, 9)]
    ledger, chunker, engine, capsule = _make(steps, min_active=2)

    _run_turns(ledger, chunker, 1, 3)
    chunker.queue_boundary_hint()  # plan write claimed a completion...
    assert chunker.boundary_hint_pending
    _run_turns(ledger, chunker, 4, 8)  # ...but stamps never change

    assert not chunker.boundary_hint_pending
    assert len(chunker.chunks) == 0
    assert engine.chunk_writes == []
    assert ledger.frozen_blocks == ()


def test_segment_size_threshold_triggers_without_milestone():
    steps = [_step(i) for i in range(1, 10)]
    ledger, chunker, engine, _ = _make(steps, min_active=2, max_steps=4, auto_capsule=True)
    _run_turns(ledger, chunker, 1, 9)

    # Single milestone, but the open segment exceeded max_steps.
    assert len(chunker.chunks) >= 1
    assert chunker.chunks[0].start_step_number == 1
    total = sum(
        c.end_step_number - c.start_step_number + 1 for c in chunker.chunks
    )
    assert total >= 4
    assert len(ledger.unchunked_turns()) >= 2  # floor respected


def test_source_token_threshold_triggers_chunk():
    steps = [_step(i) for i in range(1, 8)]
    ledger, chunker, _, _ = _make(
        steps, min_active=2, max_steps=100, target_tokens=10, auto_capsule=True
    )
    _run_turns(ledger, chunker, 1, 7)
    assert len(chunker.chunks) >= 1


def test_soft_threshold_compresses_oldest_open_segment():
    steps = [_step(i) for i in range(1, 9)]
    meter = {"value": None}
    ledger, chunker, _, _ = _make(
        steps, min_active=2, max_steps=100, meter=lambda: meter["value"], auto_capsule=True
    )
    _run_turns(ledger, chunker, 1, 6)
    assert len(chunker.chunks) == 0  # nothing due yet

    meter["value"] = 75_000  # ≥ 0.7 * 100k, < 0.9 * 100k
    _run_turns(ledger, chunker, 7, 7)
    assert len(chunker.chunks) == 1
    assert chunker.chunks[0].start_step_number == 1


def test_min_active_floor_blocks_all_triggers():
    steps = [_step(i, "hash-a" if i <= 2 else "hash-b") for i in range(1, 6)]
    ledger, chunker, engine, _ = _make(
        steps, min_active=5, max_steps=2, meter=lambda: 95_000
    )
    _run_turns(ledger, chunker, 1, 6, _hashes(2))
    # Only 5 committed turns; the floor keeps everything raw.
    assert len(chunker.chunks) == 0
    assert chunker.awaiting_chunks == ()
    assert engine.chunk_writes == []


def test_compression_boundary_never_splits_tool_call_pairs():
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 9)]
    ledger, chunker, _, _ = _make(steps, min_active=2, auto_capsule=True)
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    assert len(chunker.chunks) == 1

    rendered = ledger.render([_observation(99)])
    # Walk the rendered list: every AIMessage with tool_calls must be
    # immediately followed by its ToolMessages.
    for idx, msg in enumerate(rendered):
        if isinstance(msg, AIMessage) and msg.tool_calls:
            following = rendered[idx + 1 : idx + 1 + len(msg.tool_calls)]
            assert [getattr(m, "tool_call_id", None) for m in following] == [
                tc["id"] for tc in msg.tool_calls
            ]
    # And no orphan ToolMessage right after the frozen blocks.
    first_active = next(
        (m for m in rendered if isinstance(m, (AIMessage, ToolMessage, HumanMessage))
         and "# CURRENT OBSERVATION" in str(getattr(m, "content", ""))),
        None,
    )
    assert first_active is not None


# ---------------------------------------------------------------------------
# Async capsule discipline: ready-gated swap, byte-stable frozen region
# ---------------------------------------------------------------------------


def test_frozen_region_stays_byte_stable_between_swap_events():
    """A pending closed segment never mutates the frozen region (its original
    turns stay live); the region re-renders only when a swap event fires."""
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 13)]
    ledger, chunker, engine, capsule = _make(steps, min_active=2)
    _run_turns(ledger, chunker, 1, 8, _hashes(4))

    # Swap chunk 1 (Steps 1–4) once its capsule is ready.
    capsule.resolve("chunk:1-4", _capsule(1, 4))
    ledger.commit_staged(step_key="s8", validator_result={"status": "success"})
    ledger.render([_observation(9)])
    assert len(chunker.chunks) == 1
    frozen_before = _frozen_text(ledger)
    assert "① Synopsis & effects" in frozen_before
    assert "② Compressed step summary" in frozen_before
    assert "- Steps 1–4: carried out the segment's taps" in frozen_before

    # A hash-c stretch closes the hash-b segment — capsule pending, so the
    # frozen text stays byte-identical and the pending chunk does not swap.
    ledger.stage_turn(_turn(9))
    chunker.on_step_stamped("s9", "hash-c")
    _run_turns(ledger, chunker, 10, 12, lambda i: "hash-c")
    assert len(chunker.awaiting_chunks) == 1
    pending = chunker.awaiting_chunks[0]
    assert pending.status == "pending"
    assert _frozen_text(ledger) == frozen_before

    # Its capsule readies: the next render swaps and re-renders the region.
    capsule.resolve(pending.capsule_key, _capsule(5, 8))
    ledger.commit_staged(step_key="s12", validator_result={"status": "success"})
    ledger.render([_observation(13)])
    assert len(chunker.chunks) == 2
    frozen_after = _frozen_text(ledger)
    assert frozen_after != frozen_before
    assert "[Chunk 2" in frozen_after

    # Ready writes bumped the DB versions (append-only).
    ready_rows = [w for w in engine.chunk_writes if w["status"] == "ready"]
    assert ready_rows and all(w["version"] == 2 for w in ready_rows)


def test_chunk_block_renders_three_bands_in_order():
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 13)]
    ledger, chunker, _, capsule = _make(steps, min_active=2)
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    capsule.resolve("chunk:1-4", _capsule(1, 4))
    _run_turns(ledger, chunker, 9, 12, lambda i: "hash-c")  # second event

    block = next(
        str(m.content) for m in ledger.frozen_blocks if "[Chunk 1" in str(m.content)
    )
    # §3.3 structure: header, then ① → ② → ③ strictly in order.
    i1 = block.index("① Synopsis & effects")
    i2 = block.index("② Compressed step summary")
    i3 = block.index("③ Step action ledger")
    assert 0 < i1 < i2 < i3
    assert "What this segment was doing:" in block
    assert "What was actually done:" in block
    assert "Effects / left behind:" in block
    # The effect line carries the notes left behind (from the capsule).
    assert "notes/login_flow.md" in block
    assert "Entry: Home screen, logged out" in block
    assert "Exit: Settings page open" in block
    assert "Verified: fact-1" in block
    # ② lines carry step references; ③ is the untouched mechanical ledger.
    assert "- Steps 1–4:" in block
    assert "- Step 1 (T+00:10): Tapped 'btn1' at [1, 1] -> executed" in block
    assert " ago" not in block


# ---------------------------------------------------------------------------
# Checkpoint post-hoc annotation
# ---------------------------------------------------------------------------


def test_checkpoint_annotation_bumps_version_and_rerenders_at_next_event():
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 13)]
    ledger, chunker, engine, capsule = _make(steps, min_active=2)
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    # Ready gating: the closed segment is still awaiting its capsule —
    # post-hoc annotations are independent of the header and land anyway.
    chunk = chunker.awaiting_chunks[0]
    version_before = chunk.version
    frozen_before = _frozen_text(ledger)

    annotated = chunker.annotate_from_checkpoint(
        "hash-a",
        [{"kind": "verify", "item_text": "logged in", "status": "failed", "evidence": "no session"}],
    )
    assert annotated
    assert chunk.version == version_before + 1
    # DB write is immediate...
    assert engine.chunk_writes[-1]["version"] == chunk.version
    assert engine.chunk_writes[-1]["band1"]["annotations"][0]["item_text"] == "logged in"
    # ...but the frozen text only re-renders at the next compression event.
    assert _frozen_text(ledger) == frozen_before

    capsule.resolve("chunk:1-4", _capsule(1, 4))
    _run_turns(ledger, chunker, 9, 12, lambda i: "hash-c")
    frozen_after = _frozen_text(ledger)
    assert "Post-hoc check results:" in frozen_after
    assert "'logged in' → failed (no session)" in frozen_after


def test_checkpoint_annotation_survives_milestone_rename_via_alias_chain():
    """Chunk identity is the step range; the subgoal hash is a label resolved
    through the alias chain — a renamed milestone still reaches its chunk."""
    import pathlib
    import tempfile

    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 9)]
    with tempfile.TemporaryDirectory() as tmp:
        base_dir = pathlib.Path(tmp)
        (base_dir / "notes").mkdir()
        (base_dir / "notes" / "subgoal_hash_chain.json").write_text(
            json.dumps({"hash-a": "hash-a-renamed"}), encoding="utf-8"
        )
        ledger, chunker, engine, _ = _make(steps, min_active=2)
        engine.base_dir = base_dir
        _run_turns(ledger, chunker, 1, 8, _hashes(4))
        assert chunker.awaiting_chunks[0].subgoal_hash == "hash-a"

        assert chunker.annotate_from_checkpoint(
            "hash-a-renamed",
            [{"kind": "verify", "item_text": "x", "status": "passed", "evidence": "ok"}],
        )
        assert chunker.awaiting_chunks[0].annotations


def test_checkpoint_annotation_ignores_unmatched_subgoal():
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 9)]
    ledger, chunker, engine, _ = _make(steps, min_active=2)
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    writes_before = len(engine.chunk_writes)
    assert not chunker.annotate_from_checkpoint(
        "hash-unknown", [{"kind": "verify", "item_text": "x", "status": "failed", "evidence": "e"}]
    )
    assert len(engine.chunk_writes) == writes_before


# ---------------------------------------------------------------------------
# Era merging + recall-only overflow + L3 snapshot
# ---------------------------------------------------------------------------


def _many_chunks(
    chunk_count: int,
    *,
    max_chunks: int,
    max_eras: int | None = None,
    injected_step: int | None = None,
):
    """Drive enough milestone switches to accumulate ``chunk_count`` chunks
    of 2 steps each (min_active=2)."""
    total_steps = chunk_count * 2 + 6
    hash_for = lambda i: f"hash-{(i - 1) // 2}"  # noqa: E731 — switch every 2 steps
    steps = []
    for i in range(1, total_steps + 1):
        extra = {"subgoal_hash": hash_for(i)}
        if injected_step == i:
            extra["injected_instruction"] = "keep account A logged in"
        steps.append(_step(i, hash_for(i), extra_metadata=extra))
    ledger, chunker, engine, capsule = _make(
        steps, min_active=2, max_chunks=max_chunks, max_eras=max_eras, auto_capsule=True
    )
    _run_turns(ledger, chunker, 1, total_steps, hash_for)
    return ledger, chunker, engine, capsule


def test_era_merge_keeps_ledgers_and_merges_headers():
    ledger, chunker, _, capsule = _many_chunks(6, max_chunks=3)
    assert len(chunker.chunks) <= 3
    assert len(chunker.eras) >= 1

    merged_eras = [e for e in chunker.eras if not e.recall_only]
    assert merged_eras
    era_text = "\n".join(
        str(m.content)
        for m in ledger.frozen_blocks
        if "① Merged synopsis" in str(m.content)
    )
    assert "① Merged synopsis (structured fields, set-merged)" in era_text
    assert "② Segment titles" in era_text
    assert "③ Step action ledger" in era_text
    # ③ is retained 1:1 for every era-folded chunk (recall-only overflow
    # aside, covered separately): every step keeps its addressable line
    # (step number + T+ time).
    for era in merged_eras:
        for chunk in era.chunks:
            for n in range(chunk.start_step_number, chunk.end_step_number + 1):
                assert f"- Step {n} (T+" in era_text


def test_era_overflow_compresses_oldest_era_to_period_paragraph():
    """§10 decision 5 (final review 2026-09-01): era overflow renders the
    oldest era as a period paragraph — step range AND start/end session
    offsets in the header, a non-empty mechanically assembled synopsis, the
    recall guidance line, and verbatim user-injection lines."""
    ledger, chunker, _, _ = _many_chunks(14, max_chunks=2, injected_step=1)
    assert len(chunker.eras) > 2
    recall_eras = [e for e in chunker.eras if e.recall_only]
    assert recall_eras, "oldest eras should have overflowed to recall-only"

    frozen = _frozen_text(ledger)
    era = recall_eras[0]
    header = (
        f"[Era {era.ordinal} | Steps {era.start_step_number}–{era.end_step_number}"
        f" | {era.chunks[0].start_offset} → {era.chunks[-1].end_offset}]"
    )
    assert header in frozen
    # The paragraph follows on the same line and is never empty.
    match = re.search(re.escape(header) + r" (.+)", frozen)
    assert match and match.group(1).strip()
    # Mechanical assembly from band ①: doing survives verbatim, note
    # references from effect are merged in, verified facts are quoted.
    assert "Working toward the login milestone." in match.group(1)
    assert "notes/login_flow.md" in match.group(1)
    assert "Verified: " in match.group(1)
    # Recall guidance line is retained.
    assert (
        f"  (Step-level ledger via recall_history for steps"
        f" {era.start_step_number}–{era.end_step_number})"
    ) in frozen
    # Never-evict: the user injection survives even the period paragraph.
    assert 'User @ Step 1: "keep account A logged in"' in frozen


def _chunk_state(
    ordinal: int,
    start: int,
    end: int,
    *,
    band1: dict | None = None,
    milestone: str | None = None,
    user_lines: tuple = (),
) -> ChunkState:
    return ChunkState(
        ordinal=ordinal,
        start_step_number=start,
        end_step_number=end,
        start_step_id=f"s{start}",
        end_step_id=f"s{end}",
        source_step_ids=[f"s{i}" for i in range(start, end + 1)],
        subgoal_hash=None,
        milestone_label=milestone,
        start_offset=f"T+{start:02d}:00",
        end_offset=f"T+{end:02d}:59",
        band3="",
        minimal_index="",
        user_lines=list(user_lines),
        status="ready" if band1 else "pending",
        band1=band1 or {},
    )


def test_period_paragraph_falls_back_per_chunk_without_band1():
    """A pending (band-①-less) chunk contributes its milestone label — or its
    step range when unlabeled — to the 'Did:' chain instead of vanishing."""
    era = EraState(
        ordinal=7,
        chunks=[
            _chunk_state(1, 1, 4, band1=_capsule(1, 4)),
            _chunk_state(2, 5, 8, milestone="Checkout flow"),  # pending, labeled
            _chunk_state(3, 9, 12),  # pending, unlabeled
        ],
        recall_only=True,
    )
    paragraph = render_era_period_paragraph(era)
    assert "Working toward the login milestone." in paragraph
    assert "Checkout flow" in paragraph
    assert "steps 9–12" in paragraph
    assert "notes/login_flow.md" in paragraph
    assert "fact-1" in paragraph


def test_period_paragraph_degrades_to_milestone_list_when_all_band1_missing():
    """With no band ① anywhere the paragraph degrades to the milestone-label
    list + step ranges — never empty — and the block still carries the range/
    offset header, the recall guidance line, and verbatim user lines."""
    era = EraState(
        ordinal=3,
        chunks=[
            _chunk_state(1, 1, 4, milestone="Login"),
            _chunk_state(2, 5, 8, user_lines=['  User @ Step 6: "stay logged in"']),
        ],
        recall_only=True,
    )
    paragraph = render_era_period_paragraph(era)
    assert paragraph.strip()
    assert paragraph == "Milestones: Login (Steps 1–4); segment (Steps 5–8)."

    block = render_era_block(era)
    assert block.startswith(f"[Era 3 | Steps 1–8 | T+01:00 → T+08:59] {paragraph}")
    assert "(Step-level ledger via recall_history for steps 1–8)" in block
    assert 'User @ Step 6: "stay logged in"' in block


def test_independent_max_eras_cap_decouples_from_max_chunks():
    """M5: ``chunking.max_eras`` caps eras independently; None follows
    ``max_chunks`` (the pre-M5 equal-value behavior, asserted by the
    overflow test above)."""
    # A generous era cap keeps every folded era's ledger despite max_chunks=2.
    _, roomy, _, _ = _many_chunks(14, max_chunks=2, max_eras=50)
    assert len(roomy.eras) > 2
    assert not [e for e in roomy.eras if e.recall_only]

    # A tight era cap overflows old eras even though max_chunks is large.
    _, tight, _, _ = _many_chunks(14, max_chunks=2, max_eras=1)
    assert [e for e in tight.eras if e.recall_only]


def test_hard_threshold_renders_l3_snapshot_with_minimal_step_index():
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 13)]
    meter = {"value": None}
    ledger, chunker, _, capsule = _make(
        steps, min_active=2, meter=lambda: meter["value"]
    )
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    capsule.resolve("chunk:1-4", _capsule(1, 4))

    meter["value"] = 95_000  # ≥ 0.9 * 100k → L3
    _run_turns(ledger, chunker, 9, 12, lambda i: "hash-c")

    frozen = _frozen_text(ledger)
    assert "[Session snapshot" in frozen
    assert "Overall goal: TEST GOAL" in frozen
    assert "Verified facts: fact-1" in frozen
    assert "Device/app state: Settings page open" in frozen
    # The minimal per-step index survives: step + time + action phrase for
    # every chunked step (steps 1–4 and the second event's chunk).
    assert "--- Step index (minimal width, per chunk) ---" in frozen
    for chunk in chunker.chunks:
        for n in range(chunk.start_step_number, chunk.end_step_number + 1):
            assert f"- Step {n} (T+" in frozen
    assert "-> executed" not in frozen.split("--- Step index")[1]


def test_step_addressability_survives_every_compression_level():
    """Hard invariant (§8, revised by §10 decision 5 final review): after L2
    chunks, era merges, and L3, every compressed step's number and
    session-offset time are directly readable; the extreme period-paragraph
    layer alone degrades to *period* addressability — step range and start/end
    offsets must be present in the header."""
    ledger, chunker, _, _ = _many_chunks(6, max_chunks=3)
    frozen = _frozen_text(ledger)
    covered_steps = [
        n
        for era in chunker.eras
        if not era.recall_only
        for c in era.chunks
        for n in range(c.start_step_number, c.end_step_number + 1)
    ] + [
        n
        for c in chunker.chunks
        for n in range(c.start_step_number, c.end_step_number + 1)
    ]
    assert covered_steps
    for n in covered_steps:
        assert f"- Step {n} (T+" in frozen, f"step {n} lost its addressable line"

    # Extreme layer: recall-only eras stay *period*-addressable — the header
    # regex pins step range and T+..→T+.. start/end offsets in place.
    overflow_ledger, overflow_chunker, _, _ = _many_chunks(14, max_chunks=2)
    overflow_frozen = _frozen_text(overflow_ledger)
    recall_eras = [e for e in overflow_chunker.eras if e.recall_only]
    assert recall_eras
    for era in recall_eras:
        pattern = (
            rf"\[Era {era.ordinal} \| Steps {era.start_step_number}"
            rf"–{era.end_step_number} \| T\+\d{{2,}}:\d{{2}} → T\+\d{{2,}}:\d{{2}}\]"
        )
        assert re.search(pattern, overflow_frozen), (
            f"era {era.ordinal} lost its period-addressable header"
        )


@pytest.mark.asyncio
async def test_capsule_retry_exhaustion_degrades_to_pending_chunk():
    """Bounded retries: a lens that never covers the range exhausts its retry
    budget, the job enters the explicit failed state, and the chunk simply
    stays pending — band ③ remains independently usable."""
    from artemis.memory.chunking import ChunkCapsuleService

    class NeverCoversLens(StepCapsuleLens):
        def __init__(self):
            super().__init__(model_name="test", llm=object())
            self.attempts = 0

        async def render(self, key, payload):
            self.attempts += 1
            return None

    lens = NeverCoversLens()
    service = ChunkCapsuleService(ctx=None, lens=lens, retry_limit=1)
    service.submit("chunk:1-4", {"start_step": 1, "end_step": 4, "steps": []})
    await service.flush()

    assert lens.attempts == 2  # 1 + retry_limit, never unbounded
    assert service.has_failed("chunk:1-4")
    assert service.get_summary("chunk:1-4") is None

    # A chunker harvesting this key leaves the chunk pending — and under
    # ready gating the segment never swaps: the original turns stay live.
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 9)]
    ledger, chunker, _, _ = _make(steps, min_active=2)
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    chunker._capsule_service = service  # type: ignore[attr-defined]
    chunker._harvest_capsules()
    assert chunker.awaiting_chunks[0].status == "pending"
    assert chunker.chunks == ()
    assert ledger.frozen_blocks == ()


# ---------------------------------------------------------------------------
# Capsule availability hardening (2026-09-01 A/B root cause: the dedicated
# `chunking.model` endpoint was down all day, so every capsule attempt died
# and all 21 chunk headers stayed pending at version 1 forever)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capsule_lens_falls_back_to_secondary_model_on_provider_outage():
    """A provider outage on the primary capsule model switches the attempt to
    the configured fallback model instead of failing the capsule outright."""

    class DeadPrimary:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, messages):
            self.calls += 1
            raise Exception(
                "503 Service Unavailable. This model is currently experiencing"
                " high demand."
            )

    class HealthyFallback:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, messages):
            self.calls += 1
            return SimpleNamespace(content=json.dumps(_capsule(1, 2)))

    primary, fallback = DeadPrimary(), HealthyFallback()
    lens = StepCapsuleLens(
        model_name="dead-model",
        llm=primary,
        fallback_model_name="healthy-model",
        fallback_llm=fallback,
    )
    result = await lens.render("chunk:1-2", {"start_step": 1, "end_step": 2, "steps": []})
    assert result is not None and json.loads(result)["doing"]
    assert primary.calls == 1
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_capsule_outage_without_fallback_exhausts_to_failed():
    """The A/B breakage shape: one dead endpoint and no fallback fails every
    attempt; the job exhausts to the explicit failed state (and the chunk
    stays pending, per test_capsule_retry_exhaustion_degrades_to_pending_chunk)."""
    from artemis.memory.chunking import ChunkCapsuleService

    class DeadLLM:
        async def ainvoke(self, messages):
            raise Exception("503 Service Unavailable")

    lens = StepCapsuleLens(model_name="dead", llm=DeadLLM())
    assert not lens.has_fallback
    service = ChunkCapsuleService(ctx=None, lens=lens, retry_limit=1)
    service._retry_delays = (0.0,)
    service.submit("chunk:1-2", {"start_step": 1, "end_step": 2, "steps": []})
    await service.flush()
    assert service.has_failed("chunk:1-2")
    assert service.get_summary("chunk:1-2") is None


def test_capsule_fallback_model_resolution_google_only_and_not_primary():
    mgr = HistoryChunkManager(
        capsule_service=StubCapsuleService(),
        chunking_config=SimpleNamespace(
            max_steps=12,
            target_source_tokens=2000,
            model="gemini-3.7-flash",
            max_chunks=8,
        ),
    )

    def ctx_with(provider, model):
        return SimpleNamespace(
            llm_config=SimpleNamespace(
                summarizer=SimpleNamespace(
                    fallback=SimpleNamespace(provider=provider, model=model)
                )
            )
        )

    assert (
        mgr._resolve_capsule_fallback_model(ctx_with("google", "gemini-3.6-flash"))
        == "gemini-3.6-flash"
    )
    # Non-google fallbacks cannot ride the raw google model path.
    assert mgr._resolve_capsule_fallback_model(ctx_with("openai", "gpt-4o-mini")) is None
    # A fallback identical to the primary adds nothing.
    assert (
        mgr._resolve_capsule_fallback_model(ctx_with("google", "gemini-3.7-flash")) is None
    )


@pytest.mark.asyncio
async def test_flush_harvests_late_capsule_and_persists_ready_version():
    """Short-session backstop: a capsule that becomes ready after the last
    render is harvested and persisted at flush; flush itself never swaps (no
    freeze at shutdown), and the next render performs the swap normally."""
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 9)]
    ledger, chunker, engine, capsule = _make(steps, min_active=2)
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    assert chunker.awaiting_chunks[0].status == "pending"
    v1_writes = len(engine.chunk_writes)

    capsule.resolve("chunk:1-4", _capsule(1, 4))
    await chunker.flush(0.1)

    chunk = chunker.awaiting_chunks[0]
    assert chunk.status == "ready"
    assert chunk.version == 2
    ready_rows = [w for w in engine.chunk_writes[v1_writes:] if w["status"] == "ready"]
    assert ready_rows and ready_rows[-1]["version"] == 2
    # Flush drains and persists but never freezes the transcript.
    assert ledger.frozen_blocks == ()

    # A later render (e.g. after a process resume) swaps normally.
    ledger.commit_staged(step_key="s8", validator_result={"status": "success"})
    ledger.render([_observation(9)])
    assert chunker.chunks and chunker.chunks[0].status == "ready"


# ---------------------------------------------------------------------------
# Ready-gated swap (user decision 2026-09-01): degradation ladder
# ---------------------------------------------------------------------------


def test_soft_pressure_alone_never_swaps_unready_segment():
    """Soft-threshold pressure closes segments but never swaps an unready one:
    correctness over tokens — the original text stays until the header lands."""
    steps = [_step(i) for i in range(1, 9)]
    meter = {"value": 75_000}  # ≥ 0.7 * 100k, < 0.9 * 100k
    ledger, chunker, _, _ = _make(
        steps, min_active=2, max_steps=100, meter=lambda: meter["value"]
    )
    _run_turns(ledger, chunker, 1, 8)

    assert len(chunker.awaiting_chunks) >= 1  # soft pressure closed a portion
    assert chunker.chunks == ()               # ...but nothing swapped
    assert ledger.frozen_blocks == ()         # original text retained


def test_failed_capsule_redispatches_and_retains_original():
    """Failure rung of the ladder: an exhausted capsule job is re-dispatched at
    the next trigger/pressure render while the original turns stay live."""
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 9)]
    meter = {"value": None}
    ledger, chunker, _, capsule = _make(steps, min_active=2, meter=lambda: meter["value"])
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    key = chunker.awaiting_chunks[0].capsule_key
    assert capsule.submitted.count(key) == 1

    # Bounded retries exhausted → explicit failed state, chunk stays pending.
    capsule._failed.add(key)
    meter["value"] = 75_000  # pressure render = a re-dispatch occasion
    ledger.commit_staged(step_key="s8", validator_result={"status": "success"})
    ledger.render([_observation(9)])

    assert capsule.submitted.count(key) == 2  # re-dispatched
    assert chunker.chunks == ()               # still not swapped
    assert ledger.frozen_blocks == ()         # original text retained


def test_hard_threshold_force_swaps_pending_chunks_into_l3():
    """Emergency path: only the hard threshold may swap pending chunks — the
    frozen region becomes the L3 snapshot and ③'s minimal index survives."""
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 9)]
    meter = {"value": None}
    ledger, chunker, _, _ = _make(steps, min_active=2, meter=lambda: meter["value"])
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    assert chunker.awaiting_chunks[0].status == "pending"
    assert ledger.frozen_blocks == ()

    meter["value"] = 95_000  # ≥ 0.9 * 100k → hard
    ledger.commit_staged(step_key="s8", validator_result={"status": "success"})
    ledger.render([_observation(9)])

    assert chunker.awaiting_chunks == ()
    assert len(chunker.chunks) >= 1
    assert all(c.status == "pending" for c in chunker.chunks)
    frozen = _frozen_text(ledger)
    assert "[Session snapshot" in frozen
    for chunk in chunker.chunks:
        for n in range(chunk.start_step_number, chunk.end_step_number + 1):
            assert f"- Step {n} (T+" in frozen
    assert len(ledger.unchunked_turns()) < 8  # turns actually consumed


# ---------------------------------------------------------------------------
# Band ① note-linkage machine check (user decision 2026-09-01)
# ---------------------------------------------------------------------------


def test_capsule_note_coverage_is_machine_checked():
    """Every note key written during the segment must surface in the band-①
    text; a missing key fails the attempt (regenerate), same rank as the ②
    interval-coverage check. Segments without notes are unaffected."""
    lens = StepCapsuleLens(model_name="test", llm=object())
    payload = {
        "start_step": 3,
        "end_step": 6,
        "steps": [
            {
                "step_number": 3,
                "note_writes": [
                    {"tool": "save_note", "key": "login_flow", "gist": "code entry"}
                ],
            },
            {
                "step_number": 5,
                "note_writes": [
                    {"tool": "append_note", "key": "prices", "gist": "totals"}
                ],
            },
        ],
    }
    capsule = _capsule(3, 6)  # effect mentions notes/login_flow.md only

    # 'prices' is missing from every band-① field → judged failed.
    assert lens.parse_capsule(json.dumps(capsule), payload) is None

    covered = dict(
        capsule,
        effect=capsule["effect"] + " Also appended notes/prices.md with the totals.",
    )
    parsed = lens.parse_capsule(json.dumps(covered), payload)
    assert parsed is not None
    assert "prices" in parsed["effect"]

    # A no-notes segment keeps the original acceptance behavior.
    no_notes = {"start_step": 3, "end_step": 6, "steps": []}
    assert lens.parse_capsule(json.dumps(capsule), no_notes) is not None


# ---------------------------------------------------------------------------
# Formal lens interface (M1 difference-b closure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lens_backed_service_uses_lens_render():
    from artemis.memory.step_memory import StepLens

    class EchoLens(StepLens):
        name = "echo"

        async def render(self, key, payload):
            return f"summary-for-{key}"

    service = StepMemoryService(ctx=None, lens=EchoLens())
    service.submit("k1", {"step_number": 1})
    await service.flush()
    assert service.get_summary("k1") == "summary-for-k1"
    assert not service.has_failed("k1")
