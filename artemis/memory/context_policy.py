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

"""ContextPolicy: the declarative per-agent compiled-history policy table (M4).

Every production caller of :func:`artemis.utils.task_tree.build_plan_and_history`
used to hard-code its visibility kwargs at the call site. This module gathers
those call sites into one declaration per agent — the rendered output for each
agent is byte-identical to the previous hard-coded call (pinned by the golden
tests in ``tests/unit/memory/test_context_policy.py``) — and adds the M4 chunk
hand-off: when the transcript flag is on and history chunks exist, the
compiled view replaces chunked-away step lines with chunk blocks.

Chunk view per policy (§10 decision 4):

- ``"full"`` (outputter / history_analyzer): the chunk's stored three-band
  rendering including the ③ full-width per-step ledger;
- ``"digest"`` (everything else): bands ① + ② with the ③ ledger replaced by
  a ``ledger via recall_history`` marker line.

Because chunks only exist when ``agent.memory.transcript.enabled`` is on, the
flag-off output of every agent stays byte-for-byte unchanged.

``agent.memory.policies`` allows per-agent field overrides, e.g.::

    "memory": {"policies": {"planner": {"last_n_detailed": 3}}}
"""

from dataclasses import dataclass, fields, replace
from typing import Any

from artemis.utils.logger import get_logger

logger = get_logger(__name__)

#: Marker appended to a digest-view chunk block in place of the ③ ledger.
DIGEST_LEDGER_MARKER_TEMPLATE = (
    "③ Step action ledger: available via recall_history (Steps {start}–{end})"
)

#: The band-③ heading inside a chunk's stored ``rendered_text``.
_BAND3_HEADING = "③ Step action ledger"


@dataclass(frozen=True)
class ContextPolicy:
    """One agent's compiled-history rendering declaration.

    Field semantics map 1:1 onto ``build_plan_and_history`` kwargs, with two
    conveniences: ``uncompressed=True`` renders every step (the historical
    ``min_summaries=len(steps)`` idiom) and ``whitelist=True`` marks policies
    whose caller supplies ``keep_subgoal_hashes`` at runtime.
    """

    strategy: str = "sliding_window"  # documentation only
    strict_milestone_pruning: bool = False
    recent_window_size: int = 3
    min_summaries: int = 5
    uncompressed: bool = False
    last_n_detailed: int = 1
    all_detailed: bool = False
    for_failure_analyzer: bool = False
    whitelist: bool = False
    chunk_view: str | None = "digest"  # "full" | "digest" | None (never chunks)


#: The eight production call sites, verbatim (kwargs recorded 2026-09-01).
CONTEXT_POLICIES: dict[str, ContextPolicy] = {
    # operator.py:996 — last_n_detailed comes from the node constructor.
    "operator": ContextPolicy(
        strategy="strict_milestone",
        strict_milestone_pruning=True,
        recent_window_size=3,
    ),
    # operator.py:341 — transcript F-region cold-start restore.
    "operator_cold_start": ContextPolicy(
        strategy="full",
        uncompressed=True,
        last_n_detailed=1,
        chunk_view=None,
    ),
    # failure_analyzer.py:361
    "failure_analyzer": ContextPolicy(
        strategy="strict_milestone",
        strict_milestone_pruning=True,
        recent_window_size=3,
        last_n_detailed=1,
        for_failure_analyzer=True,
    ),
    # planner.py:133 — caller passes an empty plan and hash "default".
    "planner": ContextPolicy(
        strategy="strict_milestone",
        strict_milestone_pruning=True,
        recent_window_size=5,
        last_n_detailed=2,
    ),
    # outputter.py:81
    "outputter": ContextPolicy(
        strategy="full",
        uncompressed=True,
        last_n_detailed=0,
        chunk_view="full",
    ),
    # history_analyzer.py:113
    "history_analyzer": ContextPolicy(
        strategy="full",
        uncompressed=True,
        last_n_detailed=1,
        chunk_view="full",
    ),
    # diagnoser.py:166 — every non-whitelist kwarg is the historical default.
    "diagnoser": ContextPolicy(strategy="milestone_whitelist", whitelist=True),
    # committee_tool.py:189
    "committee": ContextPolicy(
        strategy="milestone_whitelist",
        whitelist=True,
        last_n_detailed=3,
    ),
}

_POLICY_FIELDS = {f.name for f in fields(ContextPolicy)}


def resolve_policy(agent: str) -> ContextPolicy:
    """The agent's policy with any ``agent.memory.policies`` overrides applied."""
    try:
        policy = CONTEXT_POLICIES[agent]
    except KeyError:
        raise KeyError(
            f"No ContextPolicy declared for agent '{agent}';"
            f" known agents: {sorted(CONTEXT_POLICIES)}"
        ) from None

    overrides: dict[str, Any] = {}
    try:
        from artemis.config import load_agent_config

        configured = (load_agent_config().memory.policies or {}).get(agent) or {}
        overrides = {k: v for k, v in configured.items() if k in _POLICY_FIELDS}
        unknown = set(configured) - _POLICY_FIELDS
        if unknown:
            logger.warning(
                f"agent.memory.policies['{agent}'] ignores unknown fields: {sorted(unknown)}"
            )
    except Exception:
        overrides = {}
    return replace(policy, **overrides) if overrides else policy


def _chunk_digest_text(rendered_text: str, start: int, end: int) -> str:
    """Bands ① + ② of a stored chunk rendering, ③ replaced by the recall marker."""
    marker = DIGEST_LEDGER_MARKER_TEMPLATE.format(start=start, end=end)
    head, sep, _ = rendered_text.partition(f"{_BAND3_HEADING}\n")
    if not sep:
        return f"{rendered_text.rstrip()}\n{marker}"
    return f"{head.rstrip()}\n\n{marker}"


def load_chunk_blocks(engine: Any, view: str | None) -> list[dict] | None:
    """The persisted history chunks rendered for the compiled view, or None.

    Returns None (→ byte-identical legacy rendering) unless the transcript
    flag is on, the engine exposes chunks, and at least one chunk exists.
    """
    if engine is None or view not in ("full", "digest"):
        return None
    try:
        from artemis.config import load_agent_config

        if not load_agent_config().memory.transcript.enabled:
            return None
    except Exception:
        return None
    try:
        rows = engine.get_history_chunks() or []
    except Exception as e:
        logger.debug(f"load_chunk_blocks: no chunks available: {e}")
        return None

    blocks: list[dict] = []
    for row in rows:
        start = getattr(row, "start_step_number", None)
        end = getattr(row, "end_step_number", None)
        rendered = getattr(row, "rendered_text", None)
        if not isinstance(start, int) or not isinstance(end, int) or not rendered:
            continue
        text = (
            rendered
            if view == "full"
            else _chunk_digest_text(rendered, start, end)
        )
        blocks.append(
            {"start_step_number": start, "end_step_number": end, "text": text}
        )
    return blocks or None


def build_history_for(
    agent: str,
    task_plan: str,
    steps: list,
    current_subgoal_hash: str,
    *,
    keep_subgoal_hashes: set | None = None,
    last_n_detailed: int | None = None,
    engine: Any = None,
) -> str:
    """Render the agent's compiled plan-and-history view per its policy.

    Args:
        agent: Key into :data:`CONTEXT_POLICIES`.
        keep_subgoal_hashes: Runtime whitelist (whitelist policies only).
        last_n_detailed: Runtime override (the operator's constructor knob).
        engine: DataEngine handing chunks to the compiled view; omit at call
            sites that must never receive chunk blocks.
    """
    from artemis.utils.task_tree import build_plan_and_history

    policy = resolve_policy(agent)
    kwargs: dict[str, Any] = {
        "min_summaries": len(steps) if policy.uncompressed else policy.min_summaries,
        "last_n_detailed": (
            policy.last_n_detailed if last_n_detailed is None else last_n_detailed
        ),
        "all_detailed": policy.all_detailed,
        "strict_milestone_pruning": policy.strict_milestone_pruning,
        "recent_window_size": policy.recent_window_size,
        "for_failure_analyzer": policy.for_failure_analyzer,
    }
    if policy.whitelist:
        kwargs["keep_subgoal_hashes"] = keep_subgoal_hashes

    chunks = load_chunk_blocks(engine, policy.chunk_view)
    if chunks:
        kwargs["chunks"] = chunks

    return build_plan_and_history(task_plan, steps, current_subgoal_hash, **kwargs)
