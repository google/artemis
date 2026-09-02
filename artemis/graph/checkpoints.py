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

"""Plan-driven checkpoint scheduling, the append-only verdict ledger, and exit
settlement bookkeeping.

Three invariants from the redesign govern everything here:

1. **Append-only history**: the Operator's context is never rolled back or
   switched; verdicts influence the future (status flips forward, findings
   injected) but never rewrite the past.
2. **Release / verdict / wrap-up separation**: fail-open only affects the
   release decision — the verdict value itself (``inconclusive``) is always
   recorded verbatim; assert failures are never repaired but always produce a
   machine-readable test result.
3. **Zero side effects inside check tasks**: state mutation happens only at
   harvest points (``execution_check_node``) and at exit settlement.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from artemis.utils.logger import get_logger
from artemis.utils.notes import append_note_content, get_note_file_path
from artemis.utils.plan_grammar import (
    CHECKBOX_LINE_RE,
    STATUS_ACTIVE,
    STATUS_DONE,
    CheckItem,
    PlanSnapshot,
    apply_finding_lines,
    parse_plan,
    subgoal_hash,
)

logger = get_logger(__name__)

LEDGER_FILENAME = "check_ledger.jsonl"
RUN_OUTCOME_FILENAME = "run_outcome.json"

# --- Config accessors (all fail-safe against a missing execution_setup) --------------


def _setup(ctx) -> Any:
    return getattr(ctx, "execution_setup", None)


def midway_checks_enabled(ctx) -> bool:
    setup = _setup(ctx)
    return bool(setup) and bool(getattr(setup, "midway_checks_enabled", False))


def final_check_enabled(ctx) -> bool:
    setup = _setup(ctx)
    return bool(setup) and bool(getattr(setup, "final_check_enabled", False))


def _setting(ctx, name: str, default):
    setup = _setup(ctx)
    value = getattr(setup, name, None) if setup else None
    return default if value is None else value


# --- Data contracts ------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceAnchor:
    """Anchors a checkpoint to the evidence available at completion time."""

    anchor_step_id: str | None
    trigger_ts: float
    plan_text: str


@dataclass(frozen=True)
class PendingCheckpoint:
    """A queued checkpoint: enqueue happens at plan-write time, spawn happens in
    ``execution_check_node`` after the turn's step is recorded."""

    checkpoint_id: str
    subgoal_text: str
    check_items: tuple[CheckItem, ...]
    plan_text: str
    trigger_ts: float


@dataclass(frozen=True)
class CheckpointRun:
    """One in-flight (or finished) check attempt held in ``ctx.checkpoint_tasks``."""

    attempt_id: str
    task: asyncio.Task
    checkpoint: PendingCheckpoint


class TestSummary(BaseModel):
    passed: int = 0
    failed: int = 0
    inconclusive: int = 0
    unchecked: int = 0
    failed_items: list[dict] = Field(default_factory=list)

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.inconclusive + self.unchecked


class RunOutcome(BaseModel):
    """Machine-readable run outcome: goal-achievement axis x test-result axis."""

    task_status: Literal["completed", "partial", "blocked"]
    tests: TestSummary


# --- Ledger (append-only) ------------------------------------------------------------


def ledger_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / LEDGER_FILENAME


def append_ledger_record(base_dir: str | Path, record: dict) -> None:
    """Appends one verdict record. Records are immutable once written; any later
    result for the same item can only be appended as a new record."""
    record = dict(record)
    record.setdefault("ts", time.time())
    path = ledger_path(base_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Failed to append check ledger record: {e}")


def read_ledger(base_dir: str | Path) -> list[dict]:
    path = ledger_path(base_dir)
    if not path.exists():
        return []
    records: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                logger.warning(f"Skipping malformed ledger line: {line[:120]}")
    except Exception as e:
        logger.error(f"Failed to read check ledger: {e}")
    return records


def has_ledger_records(base_dir: str | Path) -> bool:
    path = ledger_path(base_dir)
    try:
        return path.exists() and path.stat().st_size > 0
    except Exception:
        return False


# --- Plan helpers --------------------------------------------------------------------


def _read_plan_text(ctx) -> str:
    if not getattr(ctx, "data_engine", None):
        return ""
    path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to read task plan: {e}")
        return ""


def _subgoal_still_current(ctx, checkpoint_id: str) -> bool:
    """The anchored subgoal's text is unchanged iff an item with the same
    content hash still exists in the current plan."""
    snapshot = parse_plan(_read_plan_text(ctx))
    return any(i.is_top_level and i.key == checkpoint_id for i in snapshot.items)


def revert_subgoal_status(ctx, subgoal_key: str) -> bool:
    """Flips the matching top-level subgoal from ``[x]`` back to ``[/]``.

    This is a forward-looking state change (never a rollback of history) and is
    written directly to the file, bypassing the wrapped note tools.
    """
    if not getattr(ctx, "data_engine", None):
        return False
    path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")
    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
        changed = False
        for idx, line in enumerate(lines):
            match = CHECKBOX_LINE_RE.match(line)
            if not match or match.group("indent"):
                continue
            if subgoal_hash(match.group("text").strip()) == subgoal_key:
                if match.group("status") == STATUS_DONE:
                    lines[idx] = re.sub(
                        r"^(\s*-\s*\[)" + re.escape(STATUS_DONE) + r"(\])",
                        lambda m: m.group(1) + STATUS_ACTIVE + m.group(2),
                        line,
                    )
                    changed = True
                break
        if changed:
            path.write_text("\n".join(lines), encoding="utf-8")
        return changed
    except Exception as e:
        logger.error(f"Failed to revert subgoal status: {e}")
        return False


# --- Verify-finding persistence (four layers; redesign "verify finding") -------------
#
# Layer 1: the append-only ledger above (single source of truth — unchanged).
# Layer 2: `- finding:` plan lines — a single-direction projection rendered by
#          :func:`sync_finding_lines`; harness code never reads them back.
# Layer 3: the system-authored `checker-<checkpoint_id>` note — full details on
#          first failure, then one appended section per attempt verdict.
# Layer 4: single-turn ``operator_feedback`` findings (unchanged).

#: Reserved note-key prefixes for system-authored checker repair logs. The
#: model-side note tool wrappers reject writes to these keys. ``checker-`` is
#: the prefix actually used for storage (a colon is not a legal filename
#: character on Windows/NTFS); ``checker:`` is reserved alongside it so the
#: conceptual spelling can never be squatted either.
CHECKER_NOTE_PREFIXES = ("checker-", "checker:")

_FINDING_HEADLINE_EVIDENCE_LIMIT = 160


def checker_note_key(checkpoint_id: str) -> str:
    """Note key of the system-authored detail/repair log for one checkpoint."""
    return f"checker-{checkpoint_id}"


def is_checker_note_key(key: str) -> bool:
    """True when a note key is reserved for system-authored checker logs."""
    return str(key or "").strip().lower().startswith(CHECKER_NOTE_PREFIXES)


def _finding_registry(ctx) -> dict:
    """checkpoint_id -> rendered headline of the unresolved verify finding.

    Kept as a dynamic context attribute (``ArtemisContext`` allows extras) so
    the registry lives exactly as long as the run.
    """
    registry = getattr(ctx, "checker_findings", None)
    if not isinstance(registry, dict):
        registry = {}
        try:
            ctx.checker_findings = registry
        except Exception:
            pass
    return registry


def _render_finding_headline(checkpoint_id: str, verify_failures: list) -> str:
    first = verify_failures[0]
    evidence = str(getattr(first, "evidence", "") or "").strip()
    if len(evidence) > _FINDING_HEADLINE_EVIDENCE_LIMIT:
        evidence = evidence[: _FINDING_HEADLINE_EVIDENCE_LIMIT - 1] + "…"
    detail = f": {evidence}" if evidence else ""
    return (
        f"verify failed — '{first.item_text}'{detail}"
        f" (details & repair log: note '{checker_note_key(checkpoint_id)}')"
    )


def sync_finding_lines(ctx) -> None:
    """Re-projects the unresolved verify findings into ``task_plan.md``.

    Deterministic merge-back protection for the ``- finding:`` channel: every
    call strips all finding lines and re-renders one per unresolved checkpoint
    under its subgoal, so a model deletion grows back and a resolved finding
    disappears. Pure projection — nothing is ever parsed back out of the plan.
    """
    if not getattr(ctx, "data_engine", None):
        return
    path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")
    if not path.exists():
        return
    try:
        content = path.read_text(encoding="utf-8")
        registry = getattr(ctx, "checker_findings", None)
        findings = dict(registry) if isinstance(registry, dict) else {}
        projected = apply_finding_lines(content, findings)
        if projected != content:
            path.write_text(projected, encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to project finding lines into task plan: {e}")


def _log_checker_note(ctx, run: CheckpointRun, verdicts: list) -> None:
    """Appends this attempt's verdicts to the checkpoint's system note.

    The note starts at the FIRST failed verify (full criterion/evidence/
    suggestion detail) and afterwards accumulates one section per attempt —
    including the eventually passing one — as a persistent repair log. It is
    written through the notes storage API directly (never the model tool
    path), stays on disk after resolution, and is discoverable via read_note /
    recall_history under the key returned by :func:`checker_note_key`.
    """
    try:
        base_dir = ctx.data_engine.base_dir
        cid = run.checkpoint.checkpoint_id
        key = checker_note_key(cid)
        note_path = get_note_file_path(base_dir, key)
        verify_failures = [v for v in verdicts if v.kind == "verify" and v.status == "failed"]
        if not note_path.exists() and not verify_failures:
            return  # the log only starts once a verify criterion has failed

        blocks: list[str] = []
        if not note_path.exists():
            blocks.append(
                f"# Checker findings — subgoal: {run.checkpoint.subgoal_text}\n"
                "System-authored verification log (append-only). Each section"
                " records one checkpoint attempt's verdicts."
            )
        outcome = "failed" if verify_failures else "passed"
        lines = [f"## attempt {run.attempt_id} — verify {outcome}"]
        for v in verdicts:
            line = f"- [{v.kind}] '{v.item_text}': {v.status} — {v.evidence}"
            suggestion = str(getattr(v, "suggestion", "") or "")
            if suggestion:
                line += f"\n  suggestion: {suggestion}"
            lines.append(line)
        blocks.append("\n".join(lines))
        append_note_content(base_dir, key, "\n\n".join(blocks))
    except Exception as e:
        logger.error(f"Failed to append checker note for {run.attempt_id}: {e}")


# --- Queueing (called from _process_plan_write; never spawns) ------------------------


def queue_checkpoints(
    ctx,
    state,
    after: PlanSnapshot,
    new_completions: list[str],
    plan_text: str,
) -> None:
    """Enqueues a pending checkpoint for every newly completed top-level subgoal
    that carries ``on_complete`` check items.

    Spawning is deliberately deferred to ``execution_check_node``: at this
    moment the turn's step is not yet recorded, so an anchor taken here would
    point at the previous turn.
    """
    if not midway_checks_enabled(ctx):
        return
    if state is not None and getattr(state, "user_stop_requested", False):
        return
    items_by_key = {i.key: i for i in after.top_level}
    for text in new_completions:
        key = subgoal_hash(text)
        item = items_by_key.get(key)
        if item is None:
            continue
        check_items = tuple(ci for ci in after.check_items_of(item) if ci.when == "on_complete")
        if not check_items:
            continue
        ctx.pending_checkpoints.append(
            PendingCheckpoint(
                checkpoint_id=key,
                subgoal_text=text,
                check_items=check_items,
                plan_text=plan_text,
                trigger_ts=time.time(),
            )
        )
        logger.info(f"Queued checkpoint for completed subgoal: {text}")


# --- Harvest (§5.2) ------------------------------------------------------------------


def _record_verdicts(
    ctx,
    run: CheckpointRun,
    verdicts: list,
    anchor_step_id: str | None,
) -> None:
    base_dir = ctx.data_engine.base_dir
    when_by_text = {(ci.kind, ci.text): ci.when for ci in run.checkpoint.check_items}
    for v in verdicts:
        append_ledger_record(
            base_dir,
            {
                "attempt_id": run.attempt_id,
                "checkpoint_id": run.checkpoint.checkpoint_id,
                "item_text": v.item_text,
                "kind": v.kind,
                "when": when_by_text.get((v.kind, v.item_text), "on_complete"),
                "status": v.status,
                "evidence": v.evidence,
                "suggestion": getattr(v, "suggestion", ""),
                "anchor_step_id": anchor_step_id,
            },
        )


def _annotate_history_chunks(ctx, run: CheckpointRun, verdicts) -> None:
    """Post-hoc chunk annotation (M3 §9): a verdict harvested after its subgoal's
    segment was chunk-compressed is appended to that chunk and bumps its
    version. Best-effort — history annotation never gates checkpoint booking."""
    try:
        chunker = getattr(getattr(ctx, "transcript_ledger", None), "chunker", None)
        if chunker is None:
            return
        payload = [
            {
                "kind": getattr(v, "kind", "check"),
                "item_text": getattr(v, "item_text", ""),
                "status": getattr(v, "status", ""),
                "evidence": getattr(v, "evidence", ""),
            }
            for v in verdicts
        ]
        chunker.annotate_from_checkpoint(run.checkpoint.checkpoint_id, payload)
    except Exception as e:
        logger.debug(f"History chunk annotation skipped: {e}")


def _record_attempt_failure(ctx, run: CheckpointRun, status: str, evidence: str) -> None:
    base_dir = ctx.data_engine.base_dir
    for ci in run.checkpoint.check_items:
        append_ledger_record(
            base_dir,
            {
                "attempt_id": run.attempt_id,
                "checkpoint_id": run.checkpoint.checkpoint_id,
                "item_text": ci.text,
                "kind": ci.kind,
                "when": ci.when,
                "status": status,
                "evidence": evidence,
                "anchor_step_id": None,
            },
        )


def harvest_run(
    ctx,
    state,
    run: CheckpointRun,
    *,
    allow_side_effects: bool,
    cancelled_status: str = "superseded",
    anchor_step_id: str | None = None,
) -> list[str]:
    """Books a finished (done/cancelled) attempt into the ledger and, when the
    verdict is still applicable, applies its execution-state side effects.

    Returns Operator-facing findings (injected as ``operator_feedback``). The
    verdict values themselves are never altered by the release decision:
    an errored verify is *released* (fail-open) but *recorded* inconclusive.
    """
    task = run.task
    findings: list[str] = []
    checkpoint = run.checkpoint

    if task.cancelled():
        evidence = (
            "attempt superseded by a newer completion of the same subgoal"
            if cancelled_status == "superseded"
            else "cancelled at exit settlement (settlement timeout)"
        )
        _record_attempt_failure(ctx, run, cancelled_status, evidence)
        return findings

    exc = task.exception()
    if exc is not None:
        status = "inconclusive"
        reason = (
            f"check timed out after {_setting(ctx, 'checkpoint_timeout', 180.0)}s"
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
            else f"check raised an exception: {exc}"
        )
        logger.warning(
            f"Checkpoint attempt {run.attempt_id} did not produce a verdict"
            f" ({reason}); releasing (fail-open) but recording inconclusive."
        )
        _record_attempt_failure(ctx, run, status, reason)
        return findings

    report = task.result()
    verdicts = list(getattr(report, "verdicts", []) or [])
    _record_verdicts(ctx, run, verdicts, anchor_step_id)
    _annotate_history_chunks(ctx, run, verdicts)
    # Layer 3: the per-checkpoint repair log accumulates every booked attempt.
    _log_checker_note(ctx, run, verdicts)

    # Applicability gate for side effects: the verdict must belong to the
    # current attempt of its checkpoint AND the anchored subgoal's text must be
    # unchanged in the current plan. Stale/mismatched verdicts are ledger-only.
    applicable = allow_side_effects and _subgoal_still_current(ctx, checkpoint.checkpoint_id)
    user_stopped = bool(state is not None and getattr(state, "user_stop_requested", False))

    verify_failures = [v for v in verdicts if v.kind == "verify" and v.status == "failed"]
    assert_failures = [v for v in verdicts if v.kind == "assert" and v.status == "failed"]

    registry = _finding_registry(ctx)
    registry_changed = False

    if verify_failures and applicable and not user_stopped:
        max_repairs = int(_setting(ctx, "checkpoint_max_repairs", 2))
        repairs = ctx.checkpoint_repairs.get(checkpoint.checkpoint_id, 0)
        if repairs < max_repairs:
            ctx.checkpoint_repairs[checkpoint.checkpoint_id] = repairs + 1
            reverted = revert_subgoal_status(ctx, checkpoint.checkpoint_id)
            # Layer 2: register the standing plan headline for this unresolved
            # finding; the projection below renders it under the subgoal.
            registry[checkpoint.checkpoint_id] = _render_finding_headline(
                checkpoint.checkpoint_id, verify_failures
            )
            registry_changed = True
            for v in verify_failures:
                suggestion = getattr(v, "suggestion", "") or ""
                findings.append(
                    f"[verify failed] Subgoal '{checkpoint.subgoal_text}' —"
                    f" criterion '{v.item_text}': {v.evidence}"
                    + (f" Suggestion: {suggestion}" if suggestion else "")
                )
            if reverted:
                findings.append(
                    f"The subgoal '{checkpoint.subgoal_text}' has been set back to"
                    " in-progress ([/]). Address the findings above, then complete"
                    " it again."
                )
        else:
            # Quota exhausted: the run is settled for this checkpoint — the
            # standing headline is retired (the ledger and the checker note
            # keep the full record).
            registry_changed = registry.pop(checkpoint.checkpoint_id, None) is not None
            logger.warning(
                f"Checkpoint {checkpoint.checkpoint_id} exhausted its repair"
                f" quota ({max_repairs}); verdict stays failed, no further"
                " repair loop."
            )
    elif not verify_failures:
        # A booked attempt without failed verify criteria resolves (or
        # releases) the checkpoint: its standing headline is retired.
        registry_changed = registry.pop(checkpoint.checkpoint_id, None) is not None

    if registry_changed:
        sync_finding_lines(ctx)

    if assert_failures:
        policy = str(_setting(ctx, "assert_failure_policy", "continue"))
        if policy == "halt":
            ctx.assert_halt = True
            logger.warning(
                "Assert failure under 'halt' policy: latching halt flag for exit settlement."
            )

    return findings


def harvest_finished_checkpoints(ctx, state) -> list[str]:
    """Non-blocking harvest: books every ``done()`` attempt, never awaits a
    running one. Returns Operator-facing findings."""
    if not getattr(ctx, "data_engine", None):
        return []
    findings: list[str] = []
    for cid, run in list(ctx.checkpoint_tasks.items()):
        if not run.task.done():
            continue
        del ctx.checkpoint_tasks[cid]
        findings.extend(harvest_run(ctx, state, run, allow_side_effects=True))
    return findings


# --- Spawn (§5.1: after record_step) -------------------------------------------------


async def spawn_pending_checkpoints(ctx, state, anchor_step_id: str | None) -> list[str]:
    """Spawns queued checkpoints against the just-recorded step, honoring the
    concurrency cap and superseding stale attempts without losing verdicts.

    Returns findings harvested from superseded-but-finished attempts.
    """
    findings: list[str] = []
    if not getattr(ctx, "data_engine", None):
        return findings
    if not midway_checks_enabled(ctx):
        return findings
    if state is not None and getattr(state, "user_stop_requested", False):
        return findings

    from artemis.agents.checker.checker import run_checkpoint_check

    max_concurrent = int(_setting(ctx, "max_concurrent_checkpoints", 3))
    timeout = float(_setting(ctx, "checkpoint_timeout", 180.0))
    goal = getattr(state, "initial_goal", "") if state is not None else ""

    while ctx.pending_checkpoints and len(ctx.checkpoint_tasks) < max_concurrent:
        pending: PendingCheckpoint = ctx.pending_checkpoints.pop(0)
        cid = pending.checkpoint_id

        old_run: CheckpointRun | None = ctx.checkpoint_tasks.pop(cid, None)
        if old_run is not None:
            if old_run.task.done():
                # A finished-but-unharvested attempt: book it first (verdicts,
                # including failures, are never dropped), ledger-only.
                findings.extend(harvest_run(ctx, state, old_run, allow_side_effects=False))
            else:
                old_run.task.cancel()
                try:
                    await old_run.task
                except (asyncio.CancelledError, Exception):
                    pass
                harvest_run(ctx, state, old_run, allow_side_effects=False)

        seq = ctx.checkpoint_attempt_seq.get(cid, 0) + 1
        ctx.checkpoint_attempt_seq[cid] = seq
        attempt_id = f"{cid}#{seq}"
        anchor = EvidenceAnchor(
            anchor_step_id=anchor_step_id,
            trigger_ts=pending.trigger_ts,
            plan_text=pending.plan_text,
        )
        task = asyncio.create_task(
            asyncio.wait_for(
                run_checkpoint_check(
                    ctx,
                    check_items=pending.check_items,
                    anchor=anchor,
                    goal=goal,
                    subgoal_text=pending.subgoal_text,
                ),
                timeout=timeout,
            )
        )
        ctx.checkpoint_tasks[cid] = CheckpointRun(
            attempt_id=attempt_id, task=task, checkpoint=pending
        )
        logger.info(f"Spawned checkpoint attempt {attempt_id} anchored at step {anchor_step_id}")

    return findings


# --- Exit settlement barrier (§5.3 phase 1) ------------------------------------------


async def settle_all_checkpoints(ctx, state) -> None:
    """The one intentional blocking wait of the whole flow: collects every
    outstanding checkpoint before exit, bounded by ``settlement_timeout``.
    Attempts still running at the deadline are cancelled and recorded
    ``unchecked`` (settlement timeout). Runs unconditionally — a started check
    must be booked before exit regardless of the final-check switch."""
    if not getattr(ctx, "data_engine", None):
        return
    runs = list(ctx.checkpoint_tasks.values())
    ctx.checkpoint_tasks.clear()
    if not runs:
        return

    timeout = float(_setting(ctx, "settlement_timeout", 120.0))
    tasks = [r.task for r in runs]
    try:
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
    except Exception as e:
        logger.error(f"Settlement wait failed: {e}")
        pending = {t for t in tasks if not t.done()}

    for t in pending:
        t.cancel()
    for t in pending:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    for run in runs:
        # Settlement is bookkeeping, not a repair point: side effects off.
        harvest_run(
            ctx,
            state,
            run,
            allow_side_effects=False,
            cancelled_status="unchecked",
        )


# --- Run outcome ---------------------------------------------------------------------

_RESOLVABLE = ("passed", "failed", "inconclusive")


def resolve_item_status(kind: str, records: list[dict]) -> str:
    """Resolves one check item's final status from its ledger records.

    Assert: the first failure is permanent — any failed record makes the item
    failed. Verify: the latest substantive verdict wins (a repaired-then-passed
    verify is passed). No substantive record at all -> unchecked.
    """
    substantive = [r for r in records if r.get("status") in _RESOLVABLE]
    if kind == "assert" and any(r.get("status") == "failed" for r in substantive):
        return "failed"
    if substantive:
        return substantive[-1]["status"]
    return "unchecked"


def compute_test_summary(check_items: list[CheckItem], records: list[dict]) -> TestSummary:
    summary = TestSummary()
    by_item: dict[tuple[str, str], list[dict]] = {}
    for r in records:
        by_item.setdefault((str(r.get("kind")), str(r.get("item_text"))), []).append(r)

    seen: set[tuple[str, str]] = set()
    all_items: list[tuple[str, str]] = []
    for ci in check_items:
        sig = (ci.kind, ci.text)
        if sig not in seen:
            seen.add(sig)
            all_items.append(sig)
    # Items that only ever existed in the ledger (e.g. their plan lines were
    # legitimately revised by the Planner) still count.
    for sig in by_item:
        if sig not in seen:
            seen.add(sig)
            all_items.append(sig)

    for kind, text in all_items:
        status = resolve_item_status(kind, by_item.get((kind, text), []))
        if status == "passed":
            summary.passed += 1
        elif status == "failed":
            summary.failed += 1
            failing = [r for r in by_item.get((kind, text), []) if r.get("status") == "failed"]
            summary.failed_items.append(
                {
                    "item_text": text,
                    "kind": kind,
                    "evidence": failing[-1].get("evidence", "") if failing else "",
                }
            )
        elif status == "inconclusive":
            summary.inconclusive += 1
        else:
            summary.unchecked += 1
    return summary


def compute_run_outcome(
    snapshot: PlanSnapshot,
    records: list[dict],
    *,
    verify_blocked: bool,
) -> RunOutcome:
    """Assembles the machine-readable run outcome.

    ``verify_blocked`` marks the "verify unmet and budget exhausted" exit —
    the goal-achievement axis then reads blocked even though the plan claims
    completion. Assert failures never change ``task_status``: a finished task
    with failed assertions is ``completed`` with ``tests.failed > 0``.
    """
    tests = compute_test_summary(list(snapshot.all_check_items), records)
    if verify_blocked:
        task_status: Literal["completed", "partial", "blocked"] = "blocked"
    elif snapshot.all_top_level_done:
        task_status = "completed"
    else:
        task_status = "partial"
    return RunOutcome(task_status=task_status, tests=tests)


def write_run_outcome(base_dir: str | Path, outcome: RunOutcome, extra: dict | None = None) -> None:
    try:
        payload = outcome.model_dump()
        if extra:
            payload.update(extra)
        path = Path(base_dir) / RUN_OUTCOME_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error(f"Failed to write run outcome: {e}")


def read_run_outcome(base_dir: str | Path) -> dict | None:
    path = Path(base_dir) / RUN_OUTCOME_FILENAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
