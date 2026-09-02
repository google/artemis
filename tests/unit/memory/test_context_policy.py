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

"""ContextPolicy table (M4): byte-exact parity with the eight historical call
sites, config overrides, and the chunk hand-off to the compiled view."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from artemis.memory.context_policy import (
    CONTEXT_POLICIES,
    DIGEST_LEDGER_MARKER_TEMPLATE,
    build_history_for,
    load_chunk_blocks,
    resolve_policy,
)
from artemis.utils.task_tree import build_plan_and_history

HASH_A = "hash-a"
HASH_B = "hash-b"

TASK_PLAN = f"- [x] First milestone <!-- {HASH_A} -->\n- [/] Second milestone <!-- {HASH_B} -->"


def _step(number: int, subgoal_hash: str, summary: str | None = None) -> dict:
    return {
        "step_id": f"id-{number}",
        "step_number": number,
        "relative_time": f"{number * 10}.0s",
        "summary": summary or f"Step {number} summary text.",
        "action_taken": {"action": "click", "target_text": f"button {number}"},
        "operator_raw_thinking": f"thinking {number}",
        "last_execution_result": {"status": "success"},
        "extra_metadata": {"subgoal_hash": subgoal_hash},
    }


STEPS = [
    _step(1, HASH_A),
    _step(2, HASH_A),
    _step(3, HASH_A),
    _step(4, HASH_B),
    _step(5, HASH_B),
    _step(6, HASH_B),
    _step(7, HASH_B),
]


# ---------------------------------------------------------------------------
# Golden parity: the policy table must reproduce every historical hard-coded
# call byte for byte (kwargs recorded verbatim from the 2026-09-01 call sites).
# ---------------------------------------------------------------------------


GOLDEN_CALLS = {
    "operator": lambda: build_plan_and_history(
        TASK_PLAN,
        STEPS,
        HASH_B,
        last_n_detailed=1,
        strict_milestone_pruning=True,
        recent_window_size=3,
    ),
    "operator_cold_start": lambda: build_plan_and_history(
        TASK_PLAN,
        STEPS,
        HASH_B,
        min_summaries=len(STEPS),
        last_n_detailed=1,
    ),
    "failure_analyzer": lambda: build_plan_and_history(
        TASK_PLAN,
        STEPS,
        HASH_B,
        last_n_detailed=1,
        strict_milestone_pruning=True,
        recent_window_size=3,
        for_failure_analyzer=True,
    ),
    "planner": lambda: build_plan_and_history(
        "",
        STEPS,
        "default",
        last_n_detailed=2,
        strict_milestone_pruning=True,
        recent_window_size=5,
    ),
    "outputter": lambda: build_plan_and_history(
        TASK_PLAN,
        STEPS,
        HASH_B,
        last_n_detailed=0,
        min_summaries=len(STEPS),
        strict_milestone_pruning=False,
    ),
    "history_analyzer": lambda: build_plan_and_history(
        TASK_PLAN,
        STEPS,
        HASH_B,
        last_n_detailed=1,
        min_summaries=len(STEPS),
    ),
    "diagnoser": lambda: build_plan_and_history(
        TASK_PLAN,
        STEPS,
        HASH_B,
        keep_subgoal_hashes={HASH_B},
    ),
    "committee": lambda: build_plan_and_history(
        TASK_PLAN,
        STEPS,
        HASH_B,
        keep_subgoal_hashes={HASH_B},
        last_n_detailed=3,
    ),
}

POLICY_CALLS = {
    "operator": lambda: build_history_for(
        "operator", TASK_PLAN, STEPS, HASH_B, last_n_detailed=1
    ),
    "operator_cold_start": lambda: build_history_for(
        "operator_cold_start", TASK_PLAN, STEPS, HASH_B
    ),
    "failure_analyzer": lambda: build_history_for(
        "failure_analyzer", TASK_PLAN, STEPS, HASH_B
    ),
    "planner": lambda: build_history_for("planner", "", STEPS, "default"),
    "outputter": lambda: build_history_for("outputter", TASK_PLAN, STEPS, HASH_B),
    "history_analyzer": lambda: build_history_for(
        "history_analyzer", TASK_PLAN, STEPS, HASH_B
    ),
    "diagnoser": lambda: build_history_for(
        "diagnoser", TASK_PLAN, STEPS, HASH_B, keep_subgoal_hashes={HASH_B}
    ),
    "committee": lambda: build_history_for(
        "committee", TASK_PLAN, STEPS, HASH_B, keep_subgoal_hashes={HASH_B}
    ),
}


@pytest.mark.parametrize("agent", sorted(GOLDEN_CALLS))
def test_policy_table_reproduces_call_site_bytes(agent):
    assert POLICY_CALLS[agent]() == GOLDEN_CALLS[agent]()


def test_every_declared_policy_is_golden_tested():
    assert set(GOLDEN_CALLS) == set(CONTEXT_POLICIES)


# ---------------------------------------------------------------------------
# Config overrides
# ---------------------------------------------------------------------------


def _config_with_policies(policies):
    return SimpleNamespace(memory=SimpleNamespace(policies=policies))


def test_resolve_policy_applies_config_override():
    cfg = _config_with_policies({"planner": {"last_n_detailed": 3, "bogus_field": 1}})
    with patch("artemis.config.load_agent_config", return_value=cfg):
        policy = resolve_policy("planner")
    assert policy.last_n_detailed == 3
    assert policy.strict_milestone_pruning is True  # untouched fields survive


def test_resolve_policy_unknown_agent_raises():
    with pytest.raises(KeyError):
        resolve_policy("no_such_agent")


# ---------------------------------------------------------------------------
# Chunk hand-off (§10 decision 4)
# ---------------------------------------------------------------------------


RENDERED_CHUNK = (
    "[Chunk 1 | Steps 1–3 | T+00:10 → T+00:30]\n"
    "\n"
    "① Synopsis & effects\n"
    "  What this segment was doing: warming up.\n"
    "\n"
    "② Compressed step summary\n"
    "  - Steps 1–3: opened the app and logged in\n"
    "\n"
    "③ Step action ledger\n"
    "- Step 1 (T+00:10): click['button 1'] -> executed\n"
    "- Step 2 (T+00:20): click['button 2'] -> executed\n"
    "- Step 3 (T+00:30): click['button 3'] -> executed"
)


class _FakeEngine:
    def __init__(self, rows):
        self._rows = rows

    def get_history_chunks(self, **kwargs):
        return self._rows


def _chunk_row():
    return SimpleNamespace(
        start_step_number=1,
        end_step_number=3,
        rendered_text=RENDERED_CHUNK,
        status="ready",
    )


def _transcript_cfg(enabled: bool):
    return SimpleNamespace(
        memory=SimpleNamespace(
            transcript=SimpleNamespace(enabled=enabled), policies={}
        )
    )


def test_flag_off_output_is_byte_identical_even_with_engine():
    engine = _FakeEngine([_chunk_row()])
    with patch("artemis.config.load_agent_config", return_value=_transcript_cfg(False)):
        with_engine = build_history_for(
            "outputter", TASK_PLAN, STEPS, HASH_B, engine=engine
        )
        without_engine = build_history_for("outputter", TASK_PLAN, STEPS, HASH_B)
    assert with_engine == without_engine
    assert "[Chunk 1" not in with_engine


def test_flag_on_full_view_renders_chunk_block_and_drops_covered_steps():
    engine = _FakeEngine([_chunk_row()])
    with patch("artemis.config.load_agent_config", return_value=_transcript_cfg(True)):
        out = build_history_for("outputter", TASK_PLAN, STEPS, HASH_B, engine=engine)
    # Full view: the whole three-band block including the ③ ledger.
    assert "[Chunk 1 | Steps 1–3" in out
    assert "③ Step action ledger" in out
    assert "- Step 2 (T+00:20)" in out
    # Covered per-step summary lines are replaced by the chunk block.
    assert "*Step 2 (Start: 20.0s)" not in out
    # Steps outside the chunk range still render.
    assert "Step 5" in out


def test_flag_on_digest_view_replaces_ledger_with_recall_marker():
    engine = _FakeEngine([_chunk_row()])
    cfg = _transcript_cfg(True)
    with patch("artemis.config.load_agent_config", return_value=cfg):
        out = build_history_for(
            "diagnoser",
            TASK_PLAN,
            STEPS,
            HASH_B,
            keep_subgoal_hashes={HASH_A, HASH_B},
            engine=engine,
        )
    assert "[Chunk 1 | Steps 1–3" in out
    assert "② Compressed step summary" in out
    marker = DIGEST_LEDGER_MARKER_TEMPLATE.format(start=1, end=3)
    assert marker in out
    assert "- Step 2 (T+00:20)" not in out  # digest carries no ③ rows


def test_chunk_view_none_never_loads_chunks():
    engine = _FakeEngine([_chunk_row()])
    with patch("artemis.config.load_agent_config", return_value=_transcript_cfg(True)):
        assert load_chunk_blocks(engine, None) is None
        # The cold-start policy declares chunk_view=None: even with an engine
        # and the flag on, its output carries no chunk blocks (the restored
        # F region must stay a pure step rendering).
        out = build_history_for(
            "operator_cold_start", TASK_PLAN, STEPS, HASH_B, engine=engine
        )
    assert CONTEXT_POLICIES["operator_cold_start"].chunk_view is None
    assert "[Chunk 1" not in out


def test_most_recent_step_is_never_suppressed_by_a_chunk():
    engine = _FakeEngine(
        [
            SimpleNamespace(
                start_step_number=1,
                end_step_number=7,  # covers every step incl. the newest
                rendered_text=RENDERED_CHUNK,
                status="ready",
            )
        ]
    )
    with patch("artemis.config.load_agent_config", return_value=_transcript_cfg(True)):
        out = build_history_for("outputter", TASK_PLAN, STEPS, HASH_B, engine=engine)
    # The most-recent step must keep its own (detailed) rendering.
    assert "Step 7" in out
