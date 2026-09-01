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

"""Single source of truth for the task-plan grammar (the "machine channel").

``task_plan.md`` is a dual-channel document:

- **Machine channel**: the checkbox micro-grammar parsed here (status chars,
  indentation, ``[Loop]`` / ``[Loop:continuous]`` tags). Harness code may only
  ever read this channel.
- **Semantic channel**: all remaining free text. Only LLM agents interpret it;
  harness code must never branch on wording.

Every deterministic decision the harness makes about the plan (termination,
loop protection, validation/checker triggers, hand-slip detection) derives
from the :class:`PlanSnapshot` produced by :func:`parse_plan`. The same
constants render :data:`PLAN_GRAMMAR_SPEC`, the grammar description injected
into agent prompts via the ``{{ plan_grammar }}`` template variable — so the
contract the agents are taught and the contract the code enforces cannot
drift apart.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

STATUS_PENDING = " "
STATUS_ACTIVE = "/"
STATUS_DONE = "x"
STATUS_BLOCKED = "!"
STATUS_CHARS = STATUS_PENDING + STATUS_ACTIVE + STATUS_DONE + STATUS_BLOCKED

LOOP_TAG = "[Loop]"
CONTINUOUS_LOOP_TAG = "[Loop:continuous]"

CHECKBOX_LINE_RE = re.compile(
    r"^(?P<indent>\s*)-\s*\[(?P<status>[" + re.escape(STATUS_CHARS) + r"])\]\s*(?P<text>.*)$"
)
_LOOP_TAG_RE = re.compile(r"\[Loop(?::continuous)?\]")
_CONTINUOUS_TAG_RE = re.compile(re.escape(CONTINUOUS_LOOP_TAG))

#: Check lines are indented attachments under a top-level subgoal
#: (``- verify: ...`` / ``- assert: ...``) or task-level free items
#: (``- assert@end: ...`` at zero indent). The optional ``@end`` suffix moves
#: the judgment moment from the anchored subgoal's completion to task exit.
CHECK_LINE_RE = re.compile(
    r"^(?P<indent>\s*)-\s*(?P<kind>verify|assert)(?P<at_end>@end)?\s*:\s*(?P<text>\S.*?)\s*$"
)


def subgoal_hash(text: str) -> str:
    """Stable identity hash for a subgoal text (used for per-subgoal artifacts)."""
    return hashlib.md5(text.strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlanItem:
    """One checkbox line of the plan's machine channel."""

    indent: int
    status: str
    text: str
    line_no: int

    @property
    def is_top_level(self) -> bool:
        return self.indent == 0

    @property
    def is_pending(self) -> bool:
        return self.status == STATUS_PENDING

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    @property
    def is_done(self) -> bool:
        return self.status == STATUS_DONE

    @property
    def is_loop(self) -> bool:
        return bool(_LOOP_TAG_RE.search(self.text))

    @property
    def is_continuous(self) -> bool:
        return bool(_CONTINUOUS_TAG_RE.search(self.text))

    @property
    def key(self) -> str:
        return subgoal_hash(self.text)


@dataclass(frozen=True)
class CheckItem:
    """One declared check line of the plan's machine channel.

    ``verify`` marks a completion acceptance criterion (a failure triggers
    repair); ``assert`` marks a test assertion (a failure is recorded as a
    legitimate test result, never repaired).
    """

    kind: Literal["verify", "assert"]
    when: Literal["on_complete", "at_end"]
    text: str
    parent_key: str | None

    @property
    def key(self) -> str:
        """Stable identity of the check line (kind + when + text + anchor)."""
        return subgoal_hash(f"{self.kind}|{self.when}|{self.parent_key}|{self.text}")


@dataclass(frozen=True)
class PlanSnapshot:
    """Read-only derived view of a plan. Never persisted, never written back."""

    items: tuple[PlanItem, ...]
    check_items: tuple[CheckItem, ...] = field(default=())

    @property
    def top_level(self) -> tuple[PlanItem, ...]:
        return tuple(i for i in self.items if i.is_top_level)

    @property
    def has_top_level(self) -> bool:
        return bool(self.top_level)

    @property
    def all_top_level_done(self) -> bool:
        top = self.top_level
        return bool(top) and all(i.is_done for i in top)

    @property
    def continuous_top_level(self) -> tuple[PlanItem, ...]:
        return tuple(i for i in self.top_level if i.is_continuous)

    @property
    def milestone_texts(self) -> tuple[str, ...]:
        return tuple(i.text.strip() for i in self.top_level)

    def first_active(self) -> PlanItem | None:
        for item in self.items:
            if item.is_active:
                return item
        return None

    def last_active(self) -> PlanItem | None:
        for item in reversed(self.items):
            if item.is_active:
                return item
        return None

    def parent_of(self, item: PlanItem) -> PlanItem | None:
        """Nearest top-level item above an indented item."""
        parent = None
        for candidate in self.items:
            if candidate.line_no >= item.line_no:
                break
            if candidate.is_top_level:
                parent = candidate
        return parent

    def check_items_of(self, item: PlanItem) -> tuple[CheckItem, ...]:
        """Check lines anchored to the given top-level subgoal."""
        return tuple(ci for ci in self.check_items if ci.parent_key == item.key)

    @property
    def task_level_check_items(self) -> tuple[CheckItem, ...]:
        return tuple(ci for ci in self.check_items if ci.parent_key is None)

    @property
    def all_check_items(self) -> tuple[CheckItem, ...]:
        return self.check_items


def parse_plan(content: str | None) -> PlanSnapshot:
    """Parses plan markdown into its machine channel. Non-checkbox lines are ignored.

    Check lines (``- verify:`` / ``- assert:``) are parsed into
    :class:`CheckItem` entries; they never contribute to the checkbox item list
    and therefore never influence milestone hashing, drift detection, or
    completion triggers.
    """
    items: list[PlanItem] = []
    checks: list[CheckItem] = []
    current_top: PlanItem | None = None
    if content:
        for line_no, line in enumerate(content.splitlines()):
            match = CHECKBOX_LINE_RE.match(line)
            if match:
                item = PlanItem(
                    indent=len(match.group("indent")),
                    status=match.group("status"),
                    text=match.group("text").strip(),
                    line_no=line_no,
                )
                items.append(item)
                if item.is_top_level:
                    current_top = item
                continue
            check_match = CHECK_LINE_RE.match(line)
            if check_match:
                indented = bool(check_match.group("indent"))
                at_end = bool(check_match.group("at_end"))
                parent_key = current_top.key if (indented and current_top) else None
                checks.append(
                    CheckItem(
                        kind=check_match.group("kind"),
                        when="at_end" if at_end else "on_complete",
                        text=check_match.group("text").strip(),
                        parent_key=parent_key,
                    )
                )
    return PlanSnapshot(items=tuple(items), check_items=tuple(checks))


def milestones_changed(before: PlanSnapshot, after: PlanSnapshot) -> bool:
    """True if the top-level milestone texts differ (status flips don't count)."""
    return before.milestone_texts != after.milestone_texts


def _check_multiset(snapshot: PlanSnapshot) -> Counter:
    return Counter((ci.kind, ci.when, ci.text, ci.parent_key) for ci in snapshot.check_items)


def check_items_changed(before: PlanSnapshot, after: PlanSnapshot) -> bool:
    """True if the multiset of check lines differs between the two snapshots."""
    return _check_multiset(before) != _check_multiset(after)


def missing_check_items(before: PlanSnapshot, after: PlanSnapshot) -> list[CheckItem]:
    """Check items present in ``before`` but absent from ``after`` (multiset diff).

    Additions in ``after`` are ignored — adding check lines is allowed; only
    deletions/rewrites of existing ones are guarded.
    """
    deficit = _check_multiset(before) - _check_multiset(after)
    missing: list[CheckItem] = []
    for ci in before.check_items:
        sig = (ci.kind, ci.when, ci.text, ci.parent_key)
        if deficit.get(sig, 0) > 0:
            deficit[sig] -= 1
            missing.append(ci)
    return missing


def render_check_line(item: CheckItem, indent: int = 2) -> str:
    suffix = "@end" if item.when == "at_end" else ""
    prefix = " " * indent if item.parent_key is not None else ""
    return f"{prefix}- {item.kind}{suffix}: {item.text}"


def restore_missing_check_items(content_before: str, content_after: str) -> str | None:
    """Deterministic guard: merge check lines deleted/rewritten by a plan write
    back into the new content.

    Returns the merged plan text, or ``None`` when nothing is missing. Lines
    whose parent subgoal still exists are re-anchored directly under it; lines
    whose parent subgoal was removed are converted to task-level ``@end`` items
    appended at the end of the plan.
    """
    before = parse_plan(content_before)
    after = parse_plan(content_after)
    missing = missing_check_items(before, after)
    if not missing:
        return None

    lines = (content_after or "").splitlines()
    top_line_by_key: dict[str, int] = {}
    for item in after.items:
        if item.is_top_level:
            top_line_by_key.setdefault(item.key, item.line_no)

    inserts: dict[int, list[str]] = {}
    tail: list[str] = []
    for ci in missing:
        if ci.parent_key is not None and ci.parent_key in top_line_by_key:
            inserts.setdefault(top_line_by_key[ci.parent_key], []).append(render_check_line(ci))
        else:
            orphan = CheckItem(kind=ci.kind, when="at_end", text=ci.text, parent_key=None)
            tail.append(render_check_line(orphan))

    merged: list[str] = []
    for line_no, line in enumerate(lines):
        merged.append(line)
        if line_no in inserts:
            merged.extend(inserts[line_no])
    merged.extend(tail)
    return "\n".join(merged) + ("\n" if (content_after or "").endswith("\n") else "")


def new_top_level_completions(before: PlanSnapshot, after: PlanSnapshot) -> list[str]:
    """Top-level texts newly marked done, in plan order."""
    before_done = {i.text for i in before.top_level if i.is_done}
    return [i.text for i in after.top_level if i.is_done and i.text not in before_done]


def unintended_milestone_edits(before: PlanSnapshot, after: PlanSnapshot) -> list[tuple[str, str]]:
    """Hand-slip signature of a full-file rewrite: positional top-level pairs
    whose status char is unchanged but whose text was reworded.

    Only defined when the milestone count is unchanged; structural changes
    (add/remove) are a declared replan and are judged by planner validation
    instead.
    """
    top_before = before.top_level
    top_after = after.top_level
    if len(top_before) != len(top_after):
        return []
    return [
        (b.text, a.text)
        for b, a in zip(top_before, top_after)
        if b.status == a.status and b.text != a.text
    ]


_PLAN_GRAMMAR_BASE = f"""### Task Plan Grammar (machine-enforced contract)
Every checklist line MUST match `<indent>- [<status>] <text>`:
- Status characters: `[{STATUS_PENDING}]` pending, `[{STATUS_ACTIVE}]` in progress, `[{STATUS_DONE}]` completed, `[{STATUS_BLOCKED}]` blocked.
- Top-level lines (no indentation) are strategic milestones; their sub-tasks are indented by 2 spaces. All other text in the note is free-form context.
- `{LOOP_TAG}` tags a BOUNDED iterative milestone: declare its exit boundary (e.g., `(Exit: <condition>; Interval: <cadence>)`) and mark it `[{STATUS_DONE}]` only once that exit condition is verifiably met.
- `{CONTINUOUS_LOOP_TAG}` tags an UNBOUNDED continuous-monitoring milestone: it must stay `[{STATUS_ACTIVE}]` and can never be marked `[{STATUS_DONE}]`, deleted, or untagged by you — the system mechanically rejects such edits. Only an explicit external stop signal injected by the user unlocks its completion.
- The task terminates only when every top-level milestone is `[{STATUS_DONE}]`."""

_CHECK_GRAMMAR_EXTENSION = """### Check Line Grammar (declared verification standards)
A top-level milestone may carry indented check lines, and the plan may end with task-level check lines at zero indentation:
- `  - verify: <expected state>` — an acceptance criterion for its parent milestone. It is judged by an independent Checker at the moment the milestone is marked completed; a failed verify triggers a repair loop.
- `  - assert: <expected observation>` — a test assertion. It is judged by the independent Checker and a failure is recorded verbatim as a legitimate test result; assertions are NEVER repaired, worked around, or satisfied by constructing state.
- `- verify@end: ...` / `- assert@end: ...` — the `@end` suffix defers judgment to task exit, using the final device state. Items without `@end` are judged with the evidence available when their anchored milestone completes.
- Capability boundary (declare honestly, never promise more): all checks are POST-HOC audits over recorded evidence. They detect and record violations after the fact; they CANNOT block an action from happening, so "B must not run before A is confirmed" ordering rules are verified retroactively, not enforced. Transient prompts that were never captured in the execution history cannot be recovered — prefer expressing expectations as persistently probeable state."""


def render_plan_grammar_spec(include_checks: bool) -> str:
    """Renders the grammar spec taught to agents.

    ``include_checks=False`` (both check gates disabled) returns exactly the
    base grammar — zero context pollution from the checking feature.
    """
    if include_checks:
        return _PLAN_GRAMMAR_BASE + "\n\n" + _CHECK_GRAMMAR_EXTENSION
    return _PLAN_GRAMMAR_BASE


#: Backward-compatible module constant: the base grammar without the check
#: extension. Prompt injection sites use :func:`render_plan_grammar_spec`.
PLAN_GRAMMAR_SPEC = _PLAN_GRAMMAR_BASE
