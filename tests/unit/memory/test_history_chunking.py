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
        "last_execution_result": {"status": "dispatched"},
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
        self.traces: list[dict] = []
        if base_dir is not None:
            self.base_dir = base_dir

    def get_agent_friendly_steps(self):
        return self.steps

    def record_history_chunk(self, **kwargs):
        self.chunk_writes.append(kwargs)
        return f"chunk-row-{len(self.chunk_writes)}"

    def record_trace(self, **kwargs):
        self.traces.append(kwargs)
        return kwargs.get("trace_id")


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
    min_steps: int = 3,
    target_tokens: int = 10**9,
    max_chunks: int = 8,
    max_eras: int | None = None,
    meter=None,
    auto_capsule: bool = False,
    start_ratio: float = 0.35,
):
    ledger = TranscriptLedger(step_memory=StepMemoryService(ctx=None))
    engine = FakeEngine(steps)
    capsule = StubCapsuleService(auto_resolve=auto_capsule)
    chunker = HistoryChunkManager(
        engine=engine,
        chunking_config=SimpleNamespace(
            max_steps=max_steps,
            min_steps=min_steps,
            target_source_tokens=target_tokens,
            model="test-model",
            max_chunks=max_chunks,
            max_eras=max_eras,
        ),
        transcript_config=SimpleNamespace(
            context_budget_tokens=100_000,
            start_ratio=start_ratio,
            soft_ratio=0.7,
            hard_ratio=0.9,
            min_active_steps=min_active,
        ),
        capsule_service=capsule,
        # No meter: the base comes from the ledger's operator-recorded prompt
        # size (None until recorded), exactly like production wiring.
        meter_getter=meter,
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
            validator_result={"status": "dispatched"} if i > 1 else None,
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
    assert lines[0] == "- Step 1 (T+00:10): Tapped 'btn1' at [1, 1] -> dispatched"
    assert lines[1].startswith("- Step 2 (T+00:20): Tapped 'btn2' at [2, 2] -> Error: boom")


def test_action_ledger_renders_incident_and_burst_on_the_step_line():
    """No repair agent: a blocked step's ledger line carries the execution
    incident phrase, and a fast-action burst lists every member."""
    burst = [
        {"action": "tap", "target_text": "player", "coordinates": [5, 6]},
        {"action": "tap", "target_text": "Skip", "coordinates": [7, 8]},
    ]
    steps = [
        _step(
            3,
            action_taken=burst,
            last_execution_result={
                "status": "failed",
                "burst": True,
                "execution": [
                    {"action": "tap", "target_text": "player"},
                    {"action": "tap", "target_text": "Skip", "attempts": ["Error: rejected"]},
                ],
                "incident": {
                    "kind": "exec_error",
                    "category": "general",
                    "reason": "Error: rejected",
                    "action": burst[1],
                    "action_description": "Tapped 'Skip' at [7, 8]",
                    "action_index": 1,
                    "burst_size": 2,
                    "consecutive_failures": 1,
                },
            },
        )
    ]
    text = build_action_ledger(steps, SESSION_START)
    lines = text.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(
        "- Step 3 (T+00:30): Fast-action burst (2 actions, unvetted):"
        " Tapped 'player' at [5, 6] -> Tapped 'Skip' at [7, 8] -> Error: Execution failed"
        " (general, consecutive failure #1) on burst action 2/2 `tap 'Skip' at [7, 8]`:"
        " Error: rejected"
    )
    assert "FA:" not in text


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
    assert "-> dispatched" not in minimal


# ---------------------------------------------------------------------------
# Band ② — machine-checked coverage + capsule parsing
# ---------------------------------------------------------------------------


def test_interval_coverage_requires_seamless_union():
    ok = [
        {"start_step": 3, "end_step": 5, "text": "a"},
        {"start_step": 6, "end_step": 6, "text": "b"},
    ]
    assert validate_interval_coverage(ok, 3, 6)
    gap = [
        {"start_step": 3, "end_step": 4, "text": "a"},
        {"start_step": 6, "end_step": 6, "text": "b"},
    ]
    assert not validate_interval_coverage(gap, 3, 6)
    overlap = [
        {"start_step": 3, "end_step": 5, "text": "a"},
        {"start_step": 5, "end_step": 6, "text": "b"},
    ]
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
    """Ready-gated swap: a milestone switch closes
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
    ledger.commit_staged(step_key="s8", validator_result={"status": "dispatched"})
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
    total = sum(c.end_step_number - c.start_step_number + 1 for c in chunker.chunks)
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
    ledger, chunker, engine, _ = _make(steps, min_active=5, max_steps=2, meter=lambda: 95_000)
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
        (
            m
            for m in rendered
            if isinstance(m, (AIMessage, ToolMessage, HumanMessage))
            and "# CURRENT OBSERVATION" in str(getattr(m, "content", ""))
        ),
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
    ledger.commit_staged(step_key="s8", validator_result={"status": "dispatched"})
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
    ledger.commit_staged(step_key="s12", validator_result={"status": "dispatched"})
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

    block = next(str(m.content) for m in ledger.frozen_blocks if "[Chunk 1" in str(m.content))
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
    assert "- Step 1 (T+00:10): Tapped 'btn1' at [1, 1] -> dispatched" in block
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
        [
            {
                "kind": "verify",
                "item_text": "logged in",
                "status": "failed",
                "evidence": "no session",
            }
        ],
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
# Era merging + recall-only overflow + L3 fold
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
        str(m.content) for m in ledger.frozen_blocks if "① Merged synopsis" in str(m.content)
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
        f"  (Step-level ledger via search_history for steps"
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
    assert "(Step-level ledger via search_history for steps 1–8)" in block
    assert 'User @ Step 6: "stay logged in"' in block


def test_independent_max_eras_cap_decouples_from_max_chunks():
    """``chunking.max_eras`` caps eras independently; None follows
    ``max_chunks`` (the pre-M5 equal-value behavior, asserted by the
    overflow test above)."""
    # A generous era cap keeps every folded era's ledger despite max_chunks=2.
    _, roomy, _, _ = _many_chunks(14, max_chunks=2, max_eras=50)
    assert len(roomy.eras) > 2
    assert not [e for e in roomy.eras if e.recall_only]

    # A tight era cap overflows old eras even though max_chunks is large.
    _, tight, _, _ = _many_chunks(14, max_chunks=2, max_eras=1)
    assert [e for e in tight.eras if e.recall_only]


def _folded_steps(chunker) -> list[int]:
    return [
        n
        for era in chunker.eras
        if era.recall_only
        for c in era.chunks
        for n in range(c.start_step_number, c.end_step_number + 1)
    ]


def test_hard_threshold_folds_frozen_region_into_recall_only_era_with_step_index():
    """L3 is a recall-only era: the period paragraph (① fields mechanically
    merged) plus the per-step minimal index of every folded chunk — step,
    session offset and action phrase, results only via search_history."""
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 13)]
    meter = {"value": None}
    ledger, chunker, _, capsule = _make(steps, min_active=2, meter=lambda: meter["value"])
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    capsule.resolve("chunk:1-4", _capsule(1, 4))

    meter["value"] = 95_000  # ≥ 0.9 * 100k → hard; the tiny turns cannot save 5k
    _run_turns(ledger, chunker, 9, 12, lambda i: "hash-c")

    assert chunker.chunks == ()
    assert chunker.eras and all(era.recall_only for era in chunker.eras)
    frozen = _frozen_text(ledger)
    assert "[Chunk" not in frozen
    assert "[Era 1 | Steps 1–" in frozen
    # The period paragraph carries the merged ① knowledge.
    assert "Working toward the login milestone." in frozen
    assert "Verified: fact-1" in frozen
    assert "(Step-level ledger via search_history for steps" in frozen
    # The minimal per-step index survives for every folded step.
    folded = _folded_steps(chunker)
    assert folded
    for n in folded:
        assert f"- Step {n} (T+" in frozen
    assert "-> dispatched" not in frozen  # minimal width: no results


def test_step_addressability_survives_every_compression_level():
    """After L2 chunks, era merges, and recall-only periods,
    every compressed step's number and session-offset time stay directly
    readable — the recall-only layer keeps the per-step minimal index — and
    the period header pins the step range and start/end offsets."""
    ledger, chunker, _, _ = _many_chunks(6, max_chunks=3)
    frozen = _frozen_text(ledger)
    covered_steps = [
        n
        for era in chunker.eras
        for c in era.chunks
        for n in range(c.start_step_number, c.end_step_number + 1)
    ] + [n for c in chunker.chunks for n in range(c.start_step_number, c.end_step_number + 1)]
    assert covered_steps
    for n in covered_steps:
        assert f"- Step {n} (T+" in frozen, f"step {n} lost its addressable line"

    # Extreme layer: recall-only eras stay period-addressable — the header
    # regex pins step range and T+..→T+.. start/end offsets in place — and
    # every member step keeps its index line.
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
    for n in _folded_steps(overflow_chunker):
        assert f"- Step {n} (T+" in overflow_frozen, f"folded step {n} lost its index line"


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
                "503 Service Unavailable. This model is currently experiencing high demand."
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
                summarizer=SimpleNamespace(fallback=SimpleNamespace(provider=provider, model=model))
            )
        )

    assert (
        mgr._resolve_capsule_fallback_model(ctx_with("google", "gemini-3.6-flash"))
        == "gemini-3.6-flash"
    )
    # Non-google fallbacks cannot ride the raw google model path.
    assert mgr._resolve_capsule_fallback_model(ctx_with("openai", "gpt-4o-mini")) is None
    # A fallback identical to the primary adds nothing.
    assert mgr._resolve_capsule_fallback_model(ctx_with("google", "gemini-3.7-flash")) is None


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
    ledger.commit_staged(step_key="s8", validator_result={"status": "dispatched"})
    ledger.render([_observation(9)])
    assert chunker.chunks and chunker.chunks[0].status == "ready"


# ---------------------------------------------------------------------------
# Ready-gated swap: degradation ladder
# ---------------------------------------------------------------------------


def test_soft_pressure_alone_never_swaps_unready_segment():
    """Soft-threshold pressure closes segments but never swaps an unready one:
    correctness over tokens — the original text stays until the header lands."""
    steps = [_step(i) for i in range(1, 9)]
    meter = {"value": 75_000}  # ≥ 0.7 * 100k, < 0.9 * 100k
    ledger, chunker, _, _ = _make(steps, min_active=2, max_steps=100, meter=lambda: meter["value"])
    _run_turns(ledger, chunker, 1, 8)

    assert len(chunker.awaiting_chunks) >= 1  # soft pressure closed a portion
    assert chunker.chunks == ()  # ...but nothing swapped
    assert ledger.frozen_blocks == ()  # original text retained


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
    ledger.commit_staged(step_key="s8", validator_result={"status": "dispatched"})
    ledger.render([_observation(9)])

    assert capsule.submitted.count(key) == 2  # re-dispatched
    assert chunker.chunks == ()  # still not swapped
    assert ledger.frozen_blocks == ()  # original text retained


def test_hard_threshold_force_swaps_pending_chunks_into_recall_only_era():
    """Emergency path: only the hard threshold may swap pending chunks — they
    fold straight into a recall-only era (no chunk block ever), the period
    paragraph degrades to the milestone/step-range list, and ③'s minimal
    index survives for every folded step."""
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 9)]
    meter = {"value": None}
    ledger, chunker, _, _ = _make(steps, min_active=2, meter=lambda: meter["value"])
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    assert chunker.awaiting_chunks[0].status == "pending"
    assert ledger.frozen_blocks == ()

    meter["value"] = 95_000  # ≥ 0.9 * 100k → hard
    ledger.commit_staged(step_key="s8", validator_result={"status": "dispatched"})
    ledger.render([_observation(9)])

    assert chunker.awaiting_chunks == ()
    assert chunker.chunks == ()
    assert len(chunker.eras) == 1 and chunker.eras[0].recall_only
    assert all(c.status == "pending" for c in chunker.eras[0].chunks)
    frozen = _frozen_text(ledger)
    assert "[Chunk" not in frozen and CHUNK_PENDING_NOTE not in frozen
    assert "[Era 1 | Steps 1–" in frozen
    assert "Milestones: segment (Steps 1–4)" in frozen  # no band ① anywhere
    folded = _folded_steps(chunker)
    assert folded
    for n in folded:
        assert f"- Step {n} (T+" in frozen
    assert len(ledger.unchunked_turns()) < 8  # turns actually consumed


# ---------------------------------------------------------------------------
# Minimum chunk length for size / pressure closes
# ---------------------------------------------------------------------------


def test_pressure_close_waits_for_min_steps_instead_of_one_turn_chunks():
    """Soft pressure with a single open segment: eligible turns age past the
    floor one per render; they accumulate to min_steps before closing, so the
    session does not spawn a one-turn chunk (one capsule call) per render."""
    steps = [_step(i) for i in range(1, 12)]
    ledger, chunker, _, _ = _make(steps, min_active=2, min_steps=3, meter=lambda: 75_000)
    _run_turns(ledger, chunker, 1, 5)  # eligible = 2 at the step-5 render
    assert chunker.awaiting_chunks == ()
    _run_turns(ledger, chunker, 6, 6)  # eligible = 3 → one 3-turn chunk
    assert [c.capsule_key for c in chunker.awaiting_chunks] == ["chunk:1-3"]
    assert chunker.awaiting_chunks[0].trigger == "pressure"
    _run_turns(ledger, chunker, 7, 8)  # eligible beyond the claim: 1, then 2
    assert [c.capsule_key for c in chunker.awaiting_chunks] == ["chunk:1-3"]
    _run_turns(ledger, chunker, 9, 9)
    assert [c.capsule_key for c in chunker.awaiting_chunks] == ["chunk:1-3", "chunk:4-6"]


def test_size_trigger_waits_for_min_steps_even_when_one_turn_exceeds_the_tokens():
    steps = [_step(i) for i in range(1, 9)]
    ledger, chunker, _, _ = _make(steps, min_active=2, min_steps=3, target_tokens=10)
    _run_turns(ledger, chunker, 1, 4)  # eligible = 1, already over the token target
    assert chunker.awaiting_chunks == ()
    _run_turns(ledger, chunker, 5, 6)  # eligible = 3
    assert [c.capsule_key for c in chunker.awaiting_chunks] == ["chunk:1-3"]
    assert chunker.awaiting_chunks[0].trigger == "size"


def test_milestone_close_is_exempt_from_min_steps():
    steps = [_step(i, "hash-a" if i <= 1 else "hash-b") for i in range(1, 8)]
    ledger, chunker, _, _ = _make(steps, min_active=2, min_steps=3)
    _run_turns(ledger, chunker, 1, 4, _hashes(1))  # the 1-step segment aged past the floor
    assert [c.capsule_key for c in chunker.awaiting_chunks] == ["chunk:1-1"]
    assert chunker.awaiting_chunks[0].trigger == "milestone"


def test_hard_threshold_waives_min_steps():
    steps = [_step(i) for i in range(1, 8)]
    meter = {"value": 75_000}
    ledger, chunker, _, _ = _make(steps, min_active=2, min_steps=3, meter=lambda: meter["value"])
    _run_turns(ledger, chunker, 1, 4)  # eligible = 1 under soft only: held back
    assert chunker.awaiting_chunks == ()
    meter["value"] = 95_000
    _run_turns(ledger, chunker, 5, 5)  # eligible = 2 < 3, but hard: closes (and folds)
    assert len(chunker.eras) == 1 and chunker.eras[0].recall_only
    assert chunker.eras[0].chunks[0].capsule_key == "chunk:1-2"


# ---------------------------------------------------------------------------
# Start gate (swap held below budget*start_ratio) and the context base
# ---------------------------------------------------------------------------


def test_start_gate_holds_ready_chunk_below_start_ratio_then_swaps():
    """A closed segment prepares its capsule, but the original turns are
    replaced only once the operator's measured context reaches
    budget*start_ratio; until then the ready chunk waits in the queue."""
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 11)]
    meter = {"value": 20_000}  # < 0.35 * 100k
    ledger, chunker, engine, _ = _make(
        steps, min_active=2, meter=lambda: meter["value"], auto_capsule=True
    )
    _run_turns(ledger, chunker, 1, 8, _hashes(4))

    # Milestone closed steps 1–4 and the capsule is ready, yet nothing swapped.
    assert [c.status for c in chunker.awaiting_chunks] == ["ready"]
    assert chunker.chunks == ()
    assert ledger.frozen_blocks == ()
    assert len(ledger.unchunked_turns()) == 7
    # The timeline line stays running with a 'held' note carrying the gate.
    held = [
        t for t in engine.traces if t["status"] == "running" and t["payload"]["args"].get("note")
    ]
    assert len(held) == 1
    args = held[0]["payload"]["args"]
    assert args["note"] == "held"
    assert (args["context_tokens"], args["context_budget"], args["swap_at_tokens"]) == (
        20_000,
        100_000,
        35_000,
    )

    # Same render cadence, context now past the gate → the ready prefix swaps.
    meter["value"] = 40_000
    _run_turns(ledger, chunker, 9, 9, _hashes(4))
    assert len(chunker.chunks) == 1
    assert chunker.chunks[0].start_step_number == 1
    assert chunker.awaiting_chunks == ()
    assert ledger.frozen_blocks != ()
    done = engine.traces[-1]
    assert done["status"] == "success"
    assert done["payload"]["args"]["context_tokens"] == 40_000 - (
        chunker.chunks[0].source_chars // 4
    ) + (chunker.chunks[0].summary_chars // 4)


def test_leading_stampless_turn_joins_the_next_segment_instead_of_pinning_the_queue():
    """A turn with no recorded step (a reply without a tool call) that lands
    right after a chunk boundary must not close as a stampless one-turn
    segment: such a chunk has no step records (``None``) and used to sit at
    the head of the awaiting queue, so no L2 swap ever happened and the hard
    line jumped straight to the fold. It joins the following segment."""
    steps = [_step(i) for i in range(1, 13)]
    meter = {"value": 50_000}  # above the start gate
    ledger, chunker, _, capsule = _make(steps, min_active=2, meter=lambda: meter["value"])
    ledger.stage_turn([_observation(0), AIMessage(content="no tool call this turn")])
    _run_turns(ledger, chunker, 1, 7, _hashes(4))  # commits turn 0 without a step key

    assert [c.capsule_key for c in chunker.awaiting_chunks] == ["chunk:1-4"]
    assert all(entry["chunk"] is not None for entry in chunker._awaiting)
    capsule.resolve("chunk:1-4", _capsule(1, 4))
    _run_turns(ledger, chunker, 8, 8, _hashes(4))

    assert [c.capsule_key for c in chunker.chunks] == ["chunk:1-4"]
    assert chunker.awaiting_chunks == ()
    assert "[Chunk 1 | Steps 1–4" in _frozen_text(ledger)
    assert "no tool call this turn" not in _rendered_text(ledger.render([_observation(9)]))


def test_partition_folds_leading_unstamped_turns_into_the_first_segment():
    ledger, chunker, _, _ = _make([_step(i) for i in range(1, 6)])
    chunker.on_step_stamped("s2", "a")
    chunker.on_step_stamped("s3", "a")
    chunker.on_step_stamped("s4", "b")
    turns = [
        {"step_key": None},
        {"step_key": "s1"},
        {"step_key": "s2"},
        {"step_key": "s3"},
        {"step_key": "s4"},
        {"step_key": None},
    ]
    segments = chunker._partition(turns)
    assert [(h, [t["step_key"] for t in ts]) for h, ts in segments] == [
        ("a", [None, "s1", "s2", "s3"]),
        ("b", ["s4", None]),
    ]
    # All unstamped: one stampless segment, never split.
    assert chunker._partition([{"step_key": None}, {"step_key": None}]) == [
        (None, [{"step_key": None}, {"step_key": None}])
    ]


def test_start_gate_zero_restores_swap_on_ready():
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 9)]
    ledger, chunker, _, _ = _make(
        steps, min_active=2, meter=lambda: 1_000, auto_capsule=True, start_ratio=0.0
    )
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    assert len(chunker.chunks) == 1
    assert chunker.awaiting_chunks == ()


def test_start_gate_unknown_base_falls_through_to_swap_on_ready():
    """No measured operator call (meter None, nothing recorded on the ledger):
    the gate cannot decide and the legacy swap-on-ready behavior stands."""
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 9)]
    ledger, chunker, _, _ = _make(steps, min_active=2, auto_capsule=True)
    assert ledger.last_prompt_tokens is None
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    assert len(chunker.chunks) == 1


def _big_turn(i: int, chars: int = 4000) -> list:
    tool_call = {"name": "click", "args": {"target": i}, "id": f"tc{i}", "type": "tool_call"}
    return [
        _observation(i),
        AIMessage(content=f"thinking {i} " + "x" * chars, tool_calls=[tool_call]),
        ToolMessage(tool_call_id=f"tc{i}", content="Action Recorded"),
    ]


def _run_big_turns(ledger, chunker, start: int, upto: int, hash_for):
    for i in range(start, upto + 1):
        ledger.commit_staged(
            step_key=f"s{i - 1}" if i > 1 else None,
            validator_result={"status": "dispatched"} if i > 1 else None,
        )
        ledger.render([_observation(i)])
        ledger.stage_turn(_big_turn(i))
        chunker.on_step_stamped(f"s{i}", hash_for(i))


def test_hard_threshold_swaps_ready_prefix_as_l2_when_that_is_enough():
    """Hard ladder rung 1: with a ready prefix whose L2 swap brings the
    estimated context back under the hard line, the swap is an ordinary L2
    event — no fold, and the younger pending segment stays awaiting."""
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 13)]
    meter = {"value": None}
    ledger, chunker, engine, capsule = _make(steps, min_active=2, meter=lambda: meter["value"])
    # Steps 1–4 (hash-a) close at the switch; steps 5–8 (hash-b) close at the
    # next switch. Turns are ~1k tokens each so the L2 swap is worth ~4k.
    _run_big_turns(ledger, chunker, 1, 10, lambda i: "a" if i <= 4 else ("b" if i <= 8 else "c"))
    keys = [c.capsule_key for c in chunker.awaiting_chunks]
    assert keys[0] == "chunk:1-4"
    capsule.resolve("chunk:1-4", _capsule(1, 4))  # ready; chunk:5-8 stays pending

    meter["value"] = 92_000  # ≥ 0.9 * 100k; minus ~4k of turns → under the line
    _run_big_turns(ledger, chunker, 11, 11, lambda i: "c")

    assert [c.capsule_key for c in chunker.chunks] == ["chunk:1-4"]
    assert [c.capsule_key for c in chunker.awaiting_chunks] == ["chunk:5-8"]
    frozen = _frozen_text(ledger)
    assert "[Era" not in frozen  # nothing folded
    assert "[Chunk 1" in frozen
    done = engine.traces[-1]
    assert done["status"] == "success"
    assert "forced" not in done["payload"]["args"]
    assert done["payload"]["args"]["context_tokens"] < 90_000


def test_hard_threshold_escalates_to_fold_when_l2_swap_is_not_enough():
    """Hard ladder rung 2: the ready prefix's L2 swap would not bring the
    context under the line → everything closed force-swaps, pending included,
    and the whole frozen region folds into one recall-only era whose index
    lists every folded step — the pending chunk's steps too."""
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 13)]
    meter = {"value": None}
    ledger, chunker, engine, capsule = _make(steps, min_active=2, meter=lambda: meter["value"])
    _run_turns(ledger, chunker, 1, 10, lambda i: "a" if i <= 4 else ("b" if i <= 8 else "c"))
    capsule.resolve("chunk:1-4", _capsule(1, 4))

    meter["value"] = 99_000  # tiny test turns cannot shave 9k tokens
    _run_turns(ledger, chunker, 11, 11, lambda i: "c")

    assert chunker.chunks == ()
    assert chunker.awaiting_chunks == ()
    assert len(chunker.eras) == 1 and chunker.eras[0].recall_only
    era = chunker.eras[0]
    assert [c.capsule_key for c in era.chunks] == ["chunk:1-4", "chunk:5-8"]
    assert [c.status for c in era.chunks] == ["ready", "pending"]
    frozen = _frozen_text(ledger)
    assert "[Chunk" not in frozen
    assert "[Era 1 | Steps 1–8" in frozen
    # The ready chunk's ① feeds the paragraph; the pending one falls back to
    # its step range — and both keep every step's index line.
    assert "Working toward the login milestone." in frozen
    assert "steps 5–8" in frozen
    for n in range(1, 9):
        assert f"- Step {n} (T+" in frozen, f"folded step {n} lost its index line"
    # Trace wording: only the chunk without a summary is a forced fold.
    done = {
        t["payload"]["args"]["start_step"]: t for t in engine.traces if t["status"] == "success"
    }
    assert "forced" not in done[1]["payload"]["args"]
    assert "folded into the period summary" in done[1]["payload"]["result"]
    assert done[5]["payload"]["args"]["forced"] is True
    assert done[5]["payload"]["result"] == (
        "Context budget reached before the summary was ready;"
        " 4 turns folded into the period summary."
    )


def test_hard_fold_is_monotonic_later_l2_swaps_append_after_the_folded_era():
    """After a hard fold, a later non-hard L2 swap appends a new chunk block
    while the folded steps stay inside the recall-only era — the fold never
    flips back to full chunk blocks."""
    steps = [_step(i) for i in range(1, 17)]
    hash_for = lambda i: "a" if i <= 4 else ("b" if i <= 8 else ("c" if i <= 12 else "d"))  # noqa: E731
    meter = {"value": None}
    ledger, chunker, _, capsule = _make(steps, min_active=2, meter=lambda: meter["value"])
    _run_turns(ledger, chunker, 1, 10, hash_for)
    capsule.resolve("chunk:1-4", _capsule(1, 4))
    meter["value"] = 99_000
    _run_turns(ledger, chunker, 11, 11, hash_for)  # hard: folds 1–4 and 5–8
    assert chunker.chunks == () and len(chunker.eras) == 1
    folded_block = _frozen_text(ledger)

    # Pressure gone (above the start gate, below soft): the next milestone
    # switch closes 9–12; once ready it swaps as an ordinary L2 chunk.
    meter["value"] = 50_000
    _run_turns(ledger, chunker, 12, 15, hash_for)
    pending = [c for c in chunker.awaiting_chunks if c.status == "pending"]
    assert [c.capsule_key for c in pending] == ["chunk:9-12"]
    capsule.resolve("chunk:9-12", _capsule(9, 12))
    _run_turns(ledger, chunker, 16, 16, hash_for)

    assert [c.capsule_key for c in chunker.chunks] == ["chunk:9-12"]
    assert len(chunker.eras) == 1 and chunker.eras[0].recall_only
    frozen = _frozen_text(ledger)
    assert frozen.startswith(folded_block)  # the folded era is byte-identical
    assert "[Chunk 3 | Steps 9–12" in frozen
    assert "[Chunk 1" not in frozen and "[Chunk 2" not in frozen
    assert frozen.index("[Era 1") < frozen.index("[Chunk 3")
    for n in range(1, 9):
        assert f"- Step {n} (T+" in frozen


def test_hard_threshold_with_nothing_awaiting_folds_frozen_region_once():
    """Hard ladder last resort: the active window is already down to the
    sliding-window floor (nothing eligible to close, nothing awaiting), yet
    the prompt sits at the hard line — the existing L2 frozen region folds
    into a recall-only era without consuming turns, and repeated hard renders
    leave the frozen blocks byte-identical."""
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 13)]
    meter = {"value": 50_000}
    ledger, chunker, _, _ = _make(
        steps, min_active=2, meter=lambda: meter["value"], auto_capsule=True
    )
    _run_turns(ledger, chunker, 1, 7, _hashes(4))
    # Chunk 1–4 swapped as L2 at the step-7 render; s5..s6 remain = the floor.
    assert [c.capsule_key for c in chunker.chunks] == ["chunk:1-4"]
    assert chunker.awaiting_chunks == ()
    assert "[Chunk 1" in _frozen_text(ledger)
    assert len(ledger.unchunked_turns()) == 2

    meter["value"] = 95_000
    ledger.render([_observation(8)])  # hard render, nothing eligible
    frozen = _frozen_text(ledger)
    assert "[Chunk 1" not in frozen and "[Era 1 | Steps 1–4" in frozen
    assert chunker.chunks == () and chunker.eras[0].recall_only
    for n in range(1, 5):
        assert f"- Step {n} (T+" in frozen
    assert len(ledger.unchunked_turns()) == 2  # no turns consumed
    folded_blocks = ledger.frozen_blocks
    ledger.render([_observation(8)])
    ledger.render([_observation(8)])
    assert ledger.frozen_blocks == folded_blocks  # byte-stable across hard renders
    assert _frozen_text(ledger) == frozen


def test_hard_threshold_overrides_start_gate():
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 9)]
    ledger, chunker, _, _ = _make(
        steps, min_active=2, meter=lambda: 95_000, auto_capsule=True, start_ratio=0.99
    )
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    assert chunker.awaiting_chunks == ()
    assert chunker.eras and all(era.recall_only for era in chunker.eras)
    assert "[Era 1 | Steps 1–" in _frozen_text(ledger)


def test_context_base_reads_the_ledger_operator_measurement_not_the_session_meter():
    """Production wiring: no meter_getter; the base is what the operator
    recorded on its own ledger. A session-wide meter reading from another
    agent's call (the 2026-09-08 trace: a 2.7k lite call right after the
    operator's 19.9k prompt) must not leak in."""
    from artemis.services.token_meter import get_meter

    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 11)]
    ledger, chunker, _, _ = _make(steps, min_active=2, auto_capsule=True)
    # Some other agent's small call lands on the session meter.
    get_meter("session-1").record({"prompt_tokens": 2_700, "completion_tokens": 10})
    # The operator's own measured prompt is well below the gate.
    ledger.record_prompt_tokens(20_000)
    assert chunker._context_base_tokens(ledger) == 20_000
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    assert chunker.chunks == ()  # held: 20k < 35k
    assert [c.status for c in chunker.awaiting_chunks] == ["ready"]

    # Operator prompt grows past the gate → swap at the next render.
    ledger.record_prompt_tokens(36_000)
    _run_turns(ledger, chunker, 9, 9, _hashes(4))
    assert len(chunker.chunks) == 1


def test_ledger_record_prompt_tokens_ignores_missing_or_non_positive():
    ledger = TranscriptLedger(step_memory=StepMemoryService(ctx=None))
    ledger.record_prompt_tokens(None)
    ledger.record_prompt_tokens(0)
    ledger.record_prompt_tokens("x")
    assert ledger.last_prompt_tokens is None
    ledger.record_prompt_tokens(1234)
    ledger.record_prompt_tokens(None)
    assert ledger.last_prompt_tokens == 1234


def test_held_note_is_fresh_after_a_retry():
    """A chunk whose capsule failed, was re-dispatched (``retrying`` note) and
    then readied while the start gate still holds it gets a fresh ``held``
    note: the re-dispatch resets the once-per-chunk held marker."""
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 14)]
    meter = {"value": 20_000}  # < 0.35 * 100k: the gate holds ready chunks
    ledger, chunker, engine, capsule = _make(steps, min_active=2, meter=lambda: meter["value"])
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    entry = chunker._awaiting[0]
    key = entry["chunk"].capsule_key
    entry["held_announced"] = True  # a stale marker must not survive a retry

    capsule._failed.add(key)
    # The hash-b segment (5–8) ages past the floor at the step-11 render and
    # closes whole: a trigger render, which is a re-dispatch occasion.
    _run_turns(ledger, chunker, 9, 11, lambda i: "hash-c")
    assert capsule.submitted.count(key) == 2
    assert entry["held_announced"] is False
    notes = [t["payload"]["args"].get("note") for t in engine.traces if t["status"] == "running"]
    assert notes[-1] == "retrying"

    capsule.resolve(key, _capsule(1, 4))
    _run_turns(ledger, chunker, 12, 12, lambda i: "hash-c")
    assert chunker.chunks == ()  # still held below the gate
    held = [
        t
        for t in engine.traces
        if t["status"] == "running"
        and t["payload"]["args"].get("note") == "held"
        and t["payload"]["args"]["start_step"] == 1
    ]
    assert len(held) == 1
    assert engine.traces.index(held[0]) > max(
        i for i, t in enumerate(engine.traces) if t["payload"]["args"].get("note") == "retrying"
    )


# ---------------------------------------------------------------------------
# Provider-less prompt estimate + session-calibrated chars→tokens ratio
# ---------------------------------------------------------------------------


def test_estimate_prompt_tokens_counts_text_images_and_tool_call_args():
    from artemis.memory.transcript import (
        IMAGE_BLOCK_TOKENS,
        estimate_prompt_tokens,
        measure_prompt_content,
    )

    messages = [
        HumanMessage(content="a" * 400),
        HumanMessage(
            content=[
                {"type": "text", "text": "b" * 80},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,IMG"}},
                {"type": "image", "source": {"data": "IMG2"}},
                {"type": "thinking", "thinking": "c" * 20},
            ]
        ),
        AIMessage(
            content="",
            tool_calls=[{"name": "click", "args": {"x": 1}, "id": "tc1", "type": "tool_call"}],
        ),
    ]
    text_chars, image_count = measure_prompt_content(messages)
    other_block = len(str({"type": "thinking", "thinking": "c" * 20}))
    assert image_count == 2
    assert text_chars == 400 + 80 + other_block + len('{"x": 1}')
    assert estimate_prompt_tokens(messages) == text_chars // 4 + 2 * IMAGE_BLOCK_TOKENS
    assert estimate_prompt_tokens([]) == 1  # never zero: an unknown base stays gated


def _measured_prompt(text_chars: int, image_count: int = 0, char: str = "a"):
    """One HumanMessage in the shape ``measure_prompt_content`` reads: a text
    block of ``text_chars`` characters and ``image_count`` image blocks."""
    blocks = [{"type": "text", "text": char * text_chars}]
    blocks += [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}
        for _ in range(image_count)
    ]
    return [HumanMessage(content=blocks)]


def test_ledger_calibrates_chars_per_token_from_measured_prompts():
    """The first measured sample of a session goes through the whole-prompt
    fallback: ``text_chars / (prompt_tokens - images * IMAGE_BLOCK_TOKENS)``,
    clamped to [1.0, 6.0]."""

    def fresh():
        return TranscriptLedger(step_memory=StepMemoryService(ctx=None))

    ledger = fresh()
    assert ledger.chars_per_token == 4.0
    assert ledger.chars_to_tokens(1000) == 1000 // 4

    # English-like prose at ~4 chars/token: the ratio is unchanged.
    ledger.record_prompt_tokens(1000, messages=[HumanMessage(content="a" * 4000)])
    assert ledger.chars_per_token == pytest.approx(4.0)
    assert ledger.chars_to_tokens(1000) == 250

    # CJK-heavy prompt measured at ~1 token per char: the ratio drops to 1.
    ledger = fresh()
    ledger.record_prompt_tokens(4000, messages=[HumanMessage(content="中" * 4000)])
    assert ledger.chars_per_token == pytest.approx(1.0)
    assert ledger.chars_to_tokens(1000) == 1000

    # Images are subtracted at their flat cost before dividing.
    ledger = fresh()
    ledger.record_prompt_tokens(1000 + 2 * 258, messages=_measured_prompt(4000, 2))
    assert ledger.chars_per_token == pytest.approx(4.0)

    # Clamp bounds: [1.0, 6.0].
    ledger = fresh()
    ledger.record_prompt_tokens(10_000, messages=[HumanMessage(content="x" * 100)])
    assert ledger.chars_per_token == 1.0
    ledger = fresh()
    ledger.record_prompt_tokens(10, messages=[HumanMessage(content="x" * 10_000)])
    assert ledger.chars_per_token == 6.0

    # Without messages only the base is recorded; the ratio is kept.
    ledger.record_prompt_tokens(777)
    assert ledger.last_prompt_tokens == 777 and ledger.chars_per_token == 6.0


def test_ledger_first_sample_uses_the_whole_prompt_fallback():
    """Before any usable difference exists the whole-prompt quotient is the
    only information available, so the first sample still applies it."""
    ledger = TranscriptLedger(step_memory=StepMemoryService(ctx=None))
    ledger.record_prompt_tokens(5000 + 3 * 258, messages=_measured_prompt(10_000, 3))
    assert ledger.chars_per_token == pytest.approx(2.0)
    assert ledger.last_prompt_tokens == 5000 + 3 * 258


def test_ledger_differential_calibration_cancels_constant_overhead():
    """Two same-image-count samples: the ratio is dchars/dtokens, and a large
    constant term in prompt_tokens (tool schemas, image tiling; 9000 tokens
    here) that the whole-prompt fallback mis-charges to the text cancels."""
    ledger = TranscriptLedger(step_memory=StepMemoryService(ctx=None))
    overhead = 9000
    ledger.record_prompt_tokens(overhead + 4000, messages=_measured_prompt(10_000, 2))
    fallback = 10_000 / (overhead + 4000 - 2 * 258)
    assert fallback < 1.0  # the fallback mis-reads ~0.8 and clamps to 1.0
    assert ledger.chars_per_token == 1.0

    # +10000 chars, +4000 tokens, same two images: 2.5 exactly.
    ledger.record_prompt_tokens(overhead + 8000, messages=_measured_prompt(20_000, 2))
    assert ledger.chars_per_token == pytest.approx(2.5)


def test_ledger_image_count_change_replaces_reference_without_updating():
    ledger = TranscriptLedger(step_memory=StepMemoryService(ctx=None))
    ledger.record_prompt_tokens(4000, messages=_measured_prompt(10_000, 2))
    # First usable pair (+10000 chars / +5000 tokens): pooled ratio 2.0.
    ledger.record_prompt_tokens(9000, messages=_measured_prompt(20_000, 2))
    assert ledger.chars_per_token == pytest.approx(2.0)

    # Different image count: no ratio update, but it becomes the reference.
    ledger.record_prompt_tokens(14_000, messages=_measured_prompt(30_000, 3))
    assert ledger.chars_per_token == pytest.approx(2.0)

    # The next 3-image sample differences against the 3-image reference
    # (+6000 chars / +2000 tokens), not against the 2-image one: pooled
    # (10000 + 6000) / (5000 + 2000).
    ledger.record_prompt_tokens(16_000, messages=_measured_prompt(36_000, 3))
    assert ledger.chars_per_token == pytest.approx(16_000 / 7000)


def test_ledger_small_moves_hold_the_reference_until_they_accumulate():
    """A sample that moved less than the delta floor (in either direction)
    neither updates the ratio nor replaces the reference: the movement keeps
    accumulating against the held reference until it is measurable."""
    from artemis.memory.transcript import MIN_CALIBRATION_DELTA_CHARS

    ledger = TranscriptLedger(step_memory=StepMemoryService(ctx=None))
    ledger.record_prompt_tokens(2500, messages=_measured_prompt(10_000, 1))
    ledger.record_prompt_tokens(7500, messages=_measured_prompt(20_000, 1))
    assert ledger.chars_per_token == pytest.approx(2.0)

    # Below the floor: no update, even though this pair's own quotient (1.0)
    # would differ wildly from the current ratio.
    small = MIN_CALIBRATION_DELTA_CHARS - 1
    ledger.record_prompt_tokens(7500 + small, messages=_measured_prompt(20_000 + small, 1))
    assert ledger.chars_per_token == pytest.approx(2.0)
    # A slight shrink in characters with a few more tokens (turn jitter) is
    # held the same way rather than resetting the baseline.
    ledger.record_prompt_tokens(7500 + small + 100, messages=_measured_prompt(20_000 + 100, 1))
    assert ledger.chars_per_token == pytest.approx(2.0)

    # Reaching the floor differences against the HELD 20000-char / 7500-token
    # reference (+2000 chars / +2000 tokens), not the previous small sample:
    # pooled (10000 + 2000) / (5000 + 2000). Consecutive differencing would
    # have seen only +1900 chars here and never calibrated.
    ledger.record_prompt_tokens(
        9500, messages=_measured_prompt(20_000 + MIN_CALIBRATION_DELTA_CHARS, 1)
    )
    assert ledger.chars_per_token == pytest.approx(12_000 / 7000)


def test_ledger_token_shrink_resets_the_reference():
    """When the measured prompt shrinks in tokens (a compaction or scrub
    landed) the sample becomes a fresh baseline without touching the pooled
    ratio; text that grows at zero token cost is unusable and also resets."""
    ledger = TranscriptLedger(step_memory=StepMemoryService(ctx=None))
    ledger.record_prompt_tokens(2500, messages=_measured_prompt(10_000, 1))
    ledger.record_prompt_tokens(7500, messages=_measured_prompt(20_000, 1))
    assert ledger.chars_per_token == pytest.approx(2.0)

    # Compaction: fewer tokens. Ratio untouched, baseline moves here.
    ledger.record_prompt_tokens(6000, messages=_measured_prompt(15_000, 1))
    assert ledger.chars_per_token == pytest.approx(2.0)
    # +2000 chars / +500 tokens against the new baseline: pooled 12000 / 5500.
    ledger.record_prompt_tokens(6500, messages=_measured_prompt(17_000, 1))
    assert ledger.chars_per_token == pytest.approx(12_000 / 5500)

    # Grew past the floor at zero token cost: not a measurement; new baseline.
    ledger.record_prompt_tokens(6500, messages=_measured_prompt(19_000, 1))
    assert ledger.chars_per_token == pytest.approx(12_000 / 5500)
    ledger.record_prompt_tokens(7500, messages=_measured_prompt(21_000, 1))
    assert ledger.chars_per_token == pytest.approx(14_000 / 6500)


def test_ledger_pools_deltas_rather_than_taking_the_last_pair():
    ledger = TranscriptLedger(step_memory=StepMemoryService(ctx=None))
    ledger.record_prompt_tokens(10_000, messages=_measured_prompt(10_000, 1))
    # Pair 1: +10000 chars / +5000 tokens (2.0).
    ledger.record_prompt_tokens(15_000, messages=_measured_prompt(20_000, 1))
    assert ledger.chars_per_token == pytest.approx(2.0)
    # Pair 2: +2000 chars / +2000 tokens (1.0). Pooled: 12000 / 7000.
    ledger.record_prompt_tokens(17_000, messages=_measured_prompt(22_000, 1))
    assert ledger.chars_per_token == pytest.approx(12_000 / 7000)
    assert ledger.chars_per_token != pytest.approx(1.0)

    # A non-positive/None value neither disturbs the pooled ratio nor the
    # remembered reference: the next sample still differences against the
    # 22000-char one (+4000 / +1000 -> pooled 16000 / 8000).
    ledger.record_prompt_tokens(None, messages=_measured_prompt(50_000, 1))
    ledger.record_prompt_tokens(0, messages=_measured_prompt(50_000, 1))
    assert ledger.chars_per_token == pytest.approx(12_000 / 7000)
    ledger.record_prompt_tokens(18_000, messages=_measured_prompt(26_000, 1))
    assert ledger.chars_per_token == pytest.approx(2.0)


def test_ledger_calibration_regression_on_real_operator_trace():
    """Thirteen consecutive Gemini Operator prompts from a real 2026-09 run
    (session 28377d06, five full-size 1080x2424 screenshots on every call,
    tool schemas bound). Differencing against a held reference pools three
    measurable spans; the per-turn moves in between (a few hundred chars,
    some slightly negative with a few hundred more tokens) are held, not
    dropped. The whole-prompt quotient for the last call reads 1.62
    chars/token; the tail's own rate is denser, so the old estimator would
    have UNDER-counted the tail and fired the size trigger late here."""
    from artemis.memory.transcript import IMAGE_BLOCK_TOKENS

    samples = [  # (text_chars, images, prompt_tokens) straight from the trace store
        (33262, 5, 20392),
        (33954, 5, 21231),  # +692 chars: held
        (36094, 5, 22853),  # +2832 chars / +2461 tokens vs 33262: pooled
        (36918, 5, 23332),  # held
        (37519, 5, 23511),  # held
        (38437, 5, 24345),  # +2343 / +1492 vs 36094: pooled
        (38917, 5, 24741),  # held
        (39332, 5, 25650),  # held
        (42111, 5, 26248),  # +3674 / +1903 vs 38437: pooled
        (43153, 5, 26910),  # held
        (43023, 5, 27605),  # chars down, tokens up (turn jitter): held
        (43027, 5, 27869),  # held
        (43638, 5, 28176),  # held
    ]
    ledger = TranscriptLedger(step_memory=StepMemoryService(ctx=None))
    for chars, images, tokens in samples:
        ledger.record_prompt_tokens(tokens, messages=_measured_prompt(chars, images))

    pooled = (2832 + 2343 + 3674) / (2461 + 1492 + 1903)
    assert ledger.chars_per_token == pytest.approx(pooled)
    assert ledger.chars_per_token == pytest.approx(1.51, abs=0.01)

    chars, images, tokens = samples[-1]
    naive = chars / (tokens - images * IMAGE_BLOCK_TOKENS)
    assert naive == pytest.approx(1.62, abs=0.01)
    assert ledger.chars_per_token != pytest.approx(naive, abs=0.05)
    assert ledger.last_prompt_tokens == tokens


def test_calibrated_ratio_scales_the_size_trigger():
    """At a calibrated 1 char/token the size trigger closes a segment at about
    a quarter of the characters it needed at the default 4 chars/token."""
    steps = [_step(i) for i in range(1, 6)]

    def eligible_chars(ledger):
        turns = ledger.unchunked_turns()
        return ledger.turn_text_chars(turns[: len(turns) - 2])  # min_active=2

    # Default ratio: the eligible text (two ~4k-char turns once turn 5 is
    # staged) is worth ~chars/4 tokens, under the 5k target → nothing closes.
    ledger, chunker, _, _ = _make(
        steps, min_active=2, min_steps=2, max_steps=100, target_tokens=5000
    )
    ledger.record_prompt_tokens(1000, messages=[HumanMessage(content="a" * 4000)])
    _run_big_turns(ledger, chunker, 1, 5, lambda i: "hash-a")
    chars = eligible_chars(ledger)
    assert chars // 4 < 5000 <= chars
    assert chunker.awaiting_chunks == ()

    # Calibrated to 1 char/token: the same text is worth ~chars tokens → closes.
    ledger, chunker, _, _ = _make(
        steps, min_active=2, min_steps=2, max_steps=100, target_tokens=5000
    )
    ledger.record_prompt_tokens(4000, messages=[HumanMessage(content="中" * 4000)])
    assert ledger.chars_per_token == pytest.approx(1.0)
    _run_big_turns(ledger, chunker, 1, 5, lambda i: "hash-a")
    assert [c.trigger for c in chunker.awaiting_chunks] == ["size"]


# ---------------------------------------------------------------------------
# Timeline announcement (compress_history trace)
# ---------------------------------------------------------------------------


def test_compression_announces_one_tool_line_from_dispatch_to_swap():
    """Dispatch and swap share a trace ID and report the change in token usage."""
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 9)]
    meter = {"value": 60_000}
    ledger, chunker, engine, capsule = _make(steps, min_active=2, meter=lambda: meter["value"])
    _run_turns(ledger, chunker, 1, 8, _hashes(4))

    assert len(engine.traces) == 1
    running = engine.traces[0]
    assert (running["type"], running["name"], running["status"]) == (
        "tool",
        "compress_history",
        "running",
    )
    args = running["payload"]["args"]
    assert (args["start_step"], args["end_step"], args["steps"]) == (1, 4, 4)
    assert args["trigger"] == "milestone"
    assert args["source_tokens"] > 0
    assert "context_tokens" not in args

    capsule.resolve("chunk:1-4", _capsule(1, 4))
    ledger.commit_staged(step_key="s8", validator_result={"status": "dispatched"})
    ledger.render([_observation(9)])

    done = engine.traces[-1]
    assert done["trace_id"] == running["trace_id"]
    assert done["status"] == "success"
    assert done["duration"] is not None and done["duration"] >= 0
    done_args = done["payload"]["args"]
    assert done_args["source_tokens"] == args["source_tokens"]
    assert done_args["summary_tokens"] > 0
    assert done_args["context_budget"] == 100_000
    assert done_args["context_estimated"] is True
    assert done_args["context_tokens"] == (
        60_000 - done_args["source_tokens"] + done_args["summary_tokens"]
    )
    assert "forced" not in done_args


def test_forced_swap_announces_fold_without_a_ratio():
    """Forced folds report estimated context usage without a summary size."""
    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 9)]
    meter = {"value": None}
    ledger, chunker, engine, _ = _make(steps, min_active=2, meter=lambda: meter["value"])
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    assert any(t["status"] == "running" for t in engine.traces)

    meter["value"] = 95_000
    ledger.commit_staged(step_key="s8", validator_result={"status": "dispatched"})
    ledger.render([_observation(9)])  # pressure closes the open tail too, then force-swaps all

    running_ids = {t["trace_id"] for t in engine.traces if t["status"] == "running"}
    done = [t for t in engine.traces if t["status"] == "success"]
    assert done and {t["trace_id"] for t in done} <= running_ids
    for trace in done:
        assert trace["payload"]["args"]["forced"] is True
        assert trace["payload"]["args"]["summary_tokens"] is None
        assert "folded into the period summary" in trace["payload"]["result"]
    last_args = done[-1]["payload"]["args"]
    assert last_args["context_budget"] == 100_000
    assert 0 < last_args["context_tokens"] < 95_000
    # The prompt-size figure appears once per swap event, on the last line.
    assert all("context_tokens" not in t["payload"]["args"] for t in done[:-1])


def test_compression_trace_carries_a_plain_language_phase_on_every_path():
    """``args.phase`` says what the compression is doing without touching the
    trace status the rest of the UI keys on: summarizing (segment closed,
    capsule generating — also while retrying), ready (capsule on hand but the
    start gate holds the swap), applied (compressed block replaced the raw
    turns), failed (capsule generation gave up, full record kept)."""
    from artemis.memory.chunking import COMPRESSION_PHASES

    assert COMPRESSION_PHASES == ("summarizing", "ready", "applied", "failed")

    steps = [_step(i, "hash-a" if i <= 4 else "hash-b") for i in range(1, 15)]
    meter = {"value": 20_000}  # < 0.35 * 100k: the start gate holds ready chunks
    ledger, chunker, engine, capsule = _make(steps, min_active=2, meter=lambda: meter["value"])
    _run_turns(ledger, chunker, 1, 8, _hashes(4))
    chunk = chunker.awaiting_chunks[0]
    key = chunk.capsule_key

    def mine():
        return [
            (t["status"], t["payload"]["args"]["phase"])
            for t in engine.traces
            if t["trace_id"] == chunk.trace_id
        ]

    # Segment closed, capsule dispatched → summarizing.
    assert mine() == [("running", "summarizing")]

    # Capsule generation exhausted → failed phase on the same trace.
    chunker._on_capsule_failed(key)
    assert mine()[-1] == ("failed", "failed")

    # Re-dispatch at the next trigger render → summarizing again (note: retrying).
    capsule._failed.add(key)
    _run_turns(ledger, chunker, 9, 11, lambda i: "hash-c")
    assert capsule.submitted.count(key) == 2
    assert mine()[-1] == ("running", "summarizing")
    retrying = [t for t in engine.traces if t["trace_id"] == chunk.trace_id][-1]
    assert retrying["payload"]["args"]["note"] == "retrying"

    # Capsule ready while the start gate holds → ready phase, status still running.
    capsule.resolve(key, _capsule(1, 4))
    _run_turns(ledger, chunker, 12, 12, lambda i: "hash-c")
    assert chunker.chunks == ()
    assert mine()[-1] == ("running", "ready")
    held = [t for t in engine.traces if t["trace_id"] == chunk.trace_id][-1]
    assert held["payload"]["args"]["note"] == "held"

    # Context past the gate → the swap applies the summary.
    meter["value"] = 40_000
    _run_turns(ledger, chunker, 13, 13, lambda i: "hash-c")
    assert [c.start_step_number for c in chunker.chunks] == [1]
    assert mine()[-1] == ("success", "applied")

    # Every announcement on every trace carries a known phase.
    assert all(t["payload"]["args"]["phase"] in COMPRESSION_PHASES for t in engine.traces)


# ---------------------------------------------------------------------------
# Band ① note-linkage machine check
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
                "note_writes": [{"tool": "save_note", "key": "login_flow", "gist": "code entry"}],
            },
            {
                "step_number": 5,
                "note_writes": [{"tool": "append_note", "key": "prices", "gist": "totals"}],
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


def test_chunk_lists_every_step_of_a_multi_action_turn():
    """A Flash turn that executed several actions records one step per action
    and registers them all on the committed turn (``step_keys``); the chunk's
    band-③ ledger must list each of them, never only the turn's first step."""
    steps = [_step(i) for i in range(1, 4)]
    _ledger, chunker, _engine, _capsule = _make(steps)
    turns = [
        {"step_key": "s1", "step_keys": ["s1", "s2"], "start": 0, "end": 4},
        {"step_key": "s3", "start": 4, "end": 7},  # legacy shape: step_key only
    ]
    chunk = chunker._create_chunk(turns, None, {s["step_id"]: s for s in steps})
    assert chunk is not None
    assert chunk.source_step_ids == ["s1", "s2", "s3"]
    assert chunk.start_step_number == 1 and chunk.end_step_number == 3
    assert "- Step 2 (T+00:20)" in chunk.band3


# ---------------------------------------------------------------------------
# Capsule source = the operator's own transcript
# ---------------------------------------------------------------------------


def _rich_turn(i: int) -> list:
    """A turn that asks the explorer before acting, with a UI list and a
    native thinking block — the parts the old per-step projection dropped."""
    from artemis.memory.transcript import PRO_UI_LIST_MARKER

    return [
        HumanMessage(
            content=[
                {"type": "text", "text": f"# CURRENT OBSERVATION [T+00:{i % 60:02d}]"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,IMG_{i}"}},
                {"type": "text", "text": f"{PRO_UI_LIST_MARKER}\n[0] Buy button"},
            ]
        ),
        AIMessage(
            content=[
                {"type": "thinking", "thinking": "need the price first"},
                {"type": "text", "text": "Ask the explorer for the price."},
            ],
            tool_calls=[
                {
                    "name": "ask_explorer",
                    "args": {"question": "price?"},
                    "id": f"tcx{i}",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(tool_call_id=f"tcx{i}", content="Price shown: ¥39"),
        AIMessage(
            content=f"thinking {i}",
            tool_calls=[
                {"name": "click", "args": {"target": i}, "id": f"tc{i}", "type": "tool_call"}
            ],
        ),
        ToolMessage(tool_call_id=f"tc{i}", content="Action Recorded"),
    ]


def _payload_with_step(capsule: StubCapsuleService, step_number: int) -> dict:
    for payload in capsule._step_inputs.values():
        if any(s.get("step_number") == step_number for s in payload.get("steps", [])):
            return payload
    raise AssertionError(f"no capsule payload carries step {step_number}")


def test_capsule_payload_carries_each_turn_transcript_as_the_operator_saw_it():
    """The segment capsule digests exactly the text it replaces: every turn's
    post-scrub transcript — reasoning (native thinking included), every tool
    call with its arguments and returned result, the validator result — not
    a hand-picked projection. Scrubbed parts (UI list, screenshot) are gone
    from the source too, because the operator no longer saw them either."""
    steps = [_step(i) for i in range(1, 7)]
    ledger, chunker, _engine, capsule = _make(steps, min_active=2, max_steps=3)
    for i in range(1, 7):
        ledger.commit_staged(
            step_key=f"s{i - 1}" if i > 1 else None,
            validator_result={"status": "dispatched"} if i > 1 else None,
        )
        ledger.render([_observation(i)])
        ledger.stage_turn(_rich_turn(i) if i == 2 else _turn(i))
        chunker.on_step_stamped(f"s{i}", "hash-a")

    payload = _payload_with_step(capsule, 2)
    turn = next(t for t in payload["turns"] if t["steps"][0]["step_number"] == 2)
    transcript = turn["transcript"]

    # What the operator saw during that turn, verbatim and in order.
    assert "(thinking) need the price first" in transcript
    assert "Ask the explorer for the price." in transcript
    assert '[tool call] ask_explorer({"question": "price?"})' in transcript
    assert "[tool result ask_explorer]\nPrice shown: ¥39" in transcript
    assert '[tool call] click({"target": 2})' in transcript
    assert "--- Action Execution Result" in transcript and "Status: dispatched" in transcript
    assert transcript.index("Price shown") < transcript.index("click(")
    # What the scrub edge already removed from the operator's view.
    assert "Buy button" not in transcript and "IMG_2" not in transcript

    # The lens request renders the turn block: step facts, then the transcript.
    lens = StepCapsuleLens(model_name="test", llm=object())
    text = lens.build_messages(payload)[1].content
    assert "## Step 2 (T+00:20)" in text
    assert "- Step 2 (T+00:20): " in text and "Visual transition of step 2." in text
    assert "Transcript as seen by the operator during this turn:" in text
    assert text.index("Recorded steps:") < text.index("Price shown: ¥39")
    assert "Reasoning excerpt" not in text  # the transcript supersedes the excerpt


def test_chunk_without_ledger_falls_back_to_per_step_projection():
    """A chunk built without a live ledger (no transcripts) keeps the
    per-step fallback rendering so the capsule can still be produced."""
    steps = [_step(i) for i in range(1, 3)]
    _ledger, chunker, _engine, capsule = _make(steps)
    turns = [{"step_key": f"s{i}", "step_keys": [f"s{i}"], "start": 0, "end": 3} for i in (1, 2)]
    chunk = chunker._create_chunk(turns, None, {s["step_id"]: s for s in steps})
    assert chunk is not None
    payload = capsule._step_inputs[chunk.capsule_key]
    assert "turns" not in payload

    text = StepCapsuleLens(model_name="test", llm=object()).build_messages(payload)[1].content
    assert "## Step 1 (T+00:10)" in text and "## Step 2 (T+00:20)" in text
    assert "Reasoning excerpt: thinking 1" in text
    assert "Transcript as seen by the operator" not in text


def test_turn_transcript_is_capped_in_place_inside_the_capsule_request():
    lens = StepCapsuleLens(model_name="test", llm=object())
    cap = StepCapsuleLens.MAX_TURN_TRANSCRIPT_CHARS
    step = {"step_number": 4, "offset": "T+00:40", "action": "tap", "outcome": "dispatched"}
    payload = {
        "start_step": 4,
        "end_step": 4,
        "steps": [step],
        "turns": [{"steps": [step], "transcript": "x" * (cap + 500)}],
    }
    text = lens.build_messages(payload)[1].content
    assert "x" * (cap + 1) not in text
    assert "500 characters cut here" in text


def test_capped_transcript_keeps_its_tail_so_the_validator_result_survives():
    """The cut lands in the middle: the head (observation + reasoning) and the
    tail (the validator result that closes every turn) both stay verbatim."""
    cap = StepCapsuleLens.MAX_TURN_TRANSCRIPT_CHARS
    tail = "\n[observation]\n--- Action Execution Result (T+09:59) ---\nStatus: failed\nError: boom"
    transcript = "HEAD-START " + "h" * (cap + 3000) + tail
    capped = StepCapsuleLens._cap_transcript(transcript)
    assert capped.startswith("HEAD-START ")
    assert capped.endswith(tail)
    assert "characters cut here" in capped
    assert len(capped) <= cap + 200  # cap plus the announcement line


def test_visual_transition_line_is_skipped_when_the_transcript_already_has_it():
    """The resolved visual summary sits verbatim in the transcript once the
    scrub edge replaced the screenshot; only an unresolved turn (placeholder
    still there) gets the DataEngine summary as a separate line."""
    from artemis.agents.flash.context_compressor import HISTORY_SUMMARY_PREFIX

    step = {
        "step_number": 4,
        "offset": "T+00:40",
        "action": "tap",
        "outcome": "dispatched",
        "visual_summary": "The cart badge changed from 0 to 1.",
    }
    resolved = f"[observation]\n{HISTORY_SUMMARY_PREFIX}The cart badge changed from 0 to 1."
    unresolved = "[observation]\n[screenshot]"
    for transcript, expected_count in ((resolved, 1), (unresolved, 1), ("", 1)):
        payload = {"start_step": 4, "end_step": 4, "steps": [step]}
        if transcript:
            payload["turns"] = [{"steps": [step], "transcript": transcript}]
        text = StepCapsuleLens(model_name="test", llm=object()).build_messages(payload)[1].content
        assert text.count("The cart badge changed from 0 to 1.") == expected_count, transcript
    resolved_text = (
        StepCapsuleLens(model_name="test", llm=object())
        .build_messages(
            {
                "start_step": 4,
                "end_step": 4,
                "steps": [step],
                "turns": [{"steps": [step], "transcript": resolved}],
            }
        )[1]
        .content
    )
    assert "Visual transition:" not in resolved_text
    unresolved_text = (
        StepCapsuleLens(model_name="test", llm=object())
        .build_messages(
            {
                "start_step": 4,
                "end_step": 4,
                "steps": [step],
                "turns": [{"steps": [step], "transcript": unresolved}],
            }
        )[1]
        .content
    )
    assert "Visual transition: The cart badge" in unresolved_text


def test_size_trigger_measures_the_rendered_transcript_the_capsule_receives():
    """``turn_text_chars`` sizes the exact text the lens gets — tool-call
    arguments included — so the source-token trigger and the capsule payload
    never disagree about how big a segment is."""
    steps = [_step(i) for i in range(1, 5)]
    ledger, chunker, _engine, capsule = _make(steps, min_active=1, target_tokens=10**9)
    _run_turns(ledger, chunker, 1, 4)
    turns = ledger.unchunked_turns()
    assert ledger.turn_text_chars(turns) == sum(len(ledger.turn_transcript(t)) for t in turns)
    assert ledger.turn_text_chars(turns[:1]) == len(ledger.turn_transcript(turns[0]))
    assert '[tool call] click({"target": 1})' in ledger.turn_transcript(turns[0])


@pytest.mark.asyncio
async def test_ready_capsule_releases_its_turn_transcripts():
    """Once a capsule landed the job is never re-run, so the service drops the
    bulky ``turns`` from the retained payload (the flat step facts stay)."""
    from artemis.memory.chunking import ChunkCapsuleService

    class EchoLens(StepCapsuleLens):
        async def render(self, key, payload):
            return json.dumps(_capsule(1, 1))

    service = ChunkCapsuleService(None, EchoLens(model_name="test", llm=object()))
    payload = {
        "start_step": 1,
        "end_step": 1,
        "step_number": 1,
        "steps": [{"step_number": 1}],
        "turns": [{"steps": [{"step_number": 1}], "transcript": "big text"}],
    }
    service.submit("chunk:1-1", payload)
    await service.flush()
    assert service.has_summary("chunk:1-1")
    assert "turns" not in service.get_job_payload("chunk:1-1")
    assert service.get_job_payload("chunk:1-1")["steps"] == [{"step_number": 1}]


def test_multi_action_turn_renders_one_block_with_every_recorded_step():
    lens = StepCapsuleLens(model_name="test", llm=object())
    steps = [
        {"step_number": n, "offset": f"T+00:{n}0", "action": f"tap btn{n}", "outcome": "dispatched"}
        for n in (7, 8)
    ]
    payload = {"start_step": 7, "end_step": 8, "steps": steps}
    payload["turns"] = [{"steps": steps, "transcript": "[operator]\nfired two taps"}]
    text = lens.build_messages(payload)[1].content
    assert "## Steps 7–8 (T+00:70 → T+00:80)" in text
    assert "- Step 7 (T+00:70): tap btn7 -> dispatched" in text
    assert "- Step 8 (T+00:80): tap btn8 -> dispatched" in text
    assert "fired two taps" in text


# ---------------------------------------------------------------------------
# Band ② must not restate band ③
# ---------------------------------------------------------------------------


def test_band2_coordinates_are_machine_checked():
    """A coordinate literal in any band-② interval text fails the attempt
    (regenerate), same rank as the coverage check: band ③ already carries
    every action's target, so ② repeating it is the ledger written twice."""
    from artemis.memory.chunking import band2_intervals_carry_coordinates

    lens = StepCapsuleLens(model_name="test", llm=object())
    payload = {"start_step": 3, "end_step": 6, "steps": []}

    with_coords = dict(
        _capsule(3, 6),
        intervals=[
            {"start_step": 3, "end_step": 5, "text": "opened three sections in turn"},
            {"start_step": 6, "end_step": 6, "text": "tapped 'Battery' at [320, 399]"},
        ],
    )
    assert band2_intervals_carry_coordinates(with_coords["intervals"])
    assert lens.parse_capsule(json.dumps(with_coords), payload) is None

    swipe_literal = dict(
        _capsule(3, 6),
        intervals=[{"start_step": 3, "end_step": 6, "text": "swiped [556, 289, 556, 124]"}],
    )
    assert lens.parse_capsule(json.dumps(swipe_literal), payload) is None

    behavior_only = dict(
        _capsule(3, 6),
        intervals=[
            {
                "start_step": 3,
                "end_step": 6,
                "text": "Opened 'Apps' and 'Battery' and returned to the main list each time;"
                " the Battery page showed 100% and 3 rows.",
            }
        ],
    )
    assert not band2_intervals_carry_coordinates(behavior_only["intervals"])
    assert lens.parse_capsule(json.dumps(behavior_only), payload) is not None


def test_capsule_prompt_forbids_band2_coordinates_and_sets_a_length_target():
    """The prompt carries the ② contract (behavior + observed result, no
    coordinates, homogeneous runs merged) and the ①+② soft length target,
    while keeping the preserve-don't-abstract rule."""
    prompt = StepCapsuleLens(model_name="test", llm=object())._prompt
    assert "NEVER include coordinates" in prompt
    assert "NEVER narrate step by step" in prompt
    assert "Merge homogeneous consecutive actions" in prompt
    assert "one third of the source text" in prompt
    assert "Preserve, don't abstract" in prompt
