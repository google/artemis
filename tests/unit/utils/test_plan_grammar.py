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

import json
from pathlib import Path

from artemis.utils.plan_grammar import (
    CONTINUOUS_LOOP_TAG,
    PLAN_GRAMMAR_SPEC,
    check_items_changed,
    milestones_changed,
    missing_check_items,
    new_top_level_completions,
    parse_plan,
    render_plan_grammar_spec,
    restore_missing_check_items,
    subgoal_hash,
    unintended_milestone_edits,
)

PLAN = """# Task Plan
Some free-form context the parser must ignore.
- [x] Open the email app
- [/] [Loop] Inspect candidates (Exit: all inspected; Interval: n/a)
  - [x] Candidate #1
  - [/] Candidate #2
- [ ] [Loop:continuous] Monitor inbox (Interval: every 5 minutes)
- [!] Blocked milestone
"""


def test_parse_plan_machine_channel_only():
    snap = parse_plan(PLAN)
    assert len(snap.items) == 6
    assert len(snap.top_level) == 4
    assert snap.milestone_texts[0] == "Open the email app"

    top = snap.top_level
    assert top[0].is_done
    assert top[1].is_active and top[1].is_loop and not top[1].is_continuous
    assert top[2].is_pending and top[2].is_loop and top[2].is_continuous
    assert top[3].status == "!"

    sub = [i for i in snap.items if not i.is_top_level]
    assert all(i.indent == 2 for i in sub)


def test_parse_plan_empty_and_none():
    assert parse_plan(None).items == ()
    assert parse_plan("").items == ()
    assert not parse_plan("just prose, no checkboxes").has_top_level


def test_snapshot_termination_properties():
    assert not parse_plan(PLAN).all_top_level_done
    done = "- [x] A\n- [x] B"
    assert parse_plan(done).all_top_level_done
    assert parse_plan("- [x] A\n- [x] [Loop:continuous] M").continuous_top_level


def test_active_item_navigation():
    snap = parse_plan(PLAN)
    assert snap.first_active().text.startswith("[Loop] Inspect")
    assert snap.last_active().text == "Candidate #2"
    parent = snap.parent_of(snap.last_active())
    assert parent is not None and parent.text.startswith("[Loop] Inspect")


def test_milestones_changed_only_on_text():
    before = parse_plan("- [ ] A\n- [ ] B")
    status_flip = parse_plan("- [x] A\n- [/] B")
    reworded = parse_plan("- [ ] A\n- [ ] B improved")
    added = parse_plan("- [ ] A\n- [ ] B\n- [ ] C")
    assert not milestones_changed(before, status_flip)
    assert milestones_changed(before, reworded)
    assert milestones_changed(before, added)


def test_new_top_level_completions_ordered():
    before = parse_plan("- [x] A\n- [/] B\n- [ ] C")
    after = parse_plan("- [x] A\n- [x] B\n- [x] C")
    assert new_top_level_completions(before, after) == ["B", "C"]
    # Sub-task completions never trigger
    sub_after = parse_plan("- [x] A\n- [/] B\n  - [x] B1\n- [ ] C")
    assert new_top_level_completions(before, sub_after) == []


def test_unintended_milestone_edits_pairing():
    before = parse_plan("- [x] A\n- [/] B\n- [ ] C")
    reword_untouched = parse_plan("- [x] A modified\n- [/] B\n- [ ] C")
    assert unintended_milestone_edits(before, reword_untouched) == [("A", "A modified")]

    # Status changed on the same line: counts as an intentional touch
    complete_and_annotate = parse_plan("- [x] A\n- [x] B (verified)\n- [ ] C")
    assert unintended_milestone_edits(before, complete_and_annotate) == []

    # Structural change: not this check's business
    removed = parse_plan("- [x] A\n- [/] B")
    assert unintended_milestone_edits(before, removed) == []


def test_subgoal_hash_stability():
    assert subgoal_hash(" A ") == subgoal_hash("A")
    item = parse_plan("- [/] Goal text").items[0]
    assert item.key == subgoal_hash("Goal text")


CHECK_PLAN = """# Task Plan
- [x] Create the 7:30 AM alarm in the Clock app
  - [ ] open clock app
  - verify: the alarm list shows 7:30 AM with its toggle ON
  - assert: an "Alarm set" toast should appear after creation
- [ ] Send the report email
  - verify: the email appears in the Sent folder
- assert@end: no application crash dialog should ever appear
- verify@end: the device is back on the home screen
"""


def test_check_lines_parse_with_anchor_and_timing():
    snap = parse_plan(CHECK_PLAN)
    checks = snap.all_check_items
    assert len(checks) == 5

    alarm = next(i for i in snap.top_level if i.text.startswith("Create the"))
    alarm_checks = snap.check_items_of(alarm)
    assert [c.kind for c in alarm_checks] == ["verify", "assert"]
    assert all(c.when == "on_complete" for c in alarm_checks)
    assert alarm_checks[0].text == "the alarm list shows 7:30 AM with its toggle ON"
    assert all(c.parent_key == alarm.key for c in alarm_checks)

    task_level = snap.task_level_check_items
    assert len(task_level) == 2
    assert all(c.when == "at_end" for c in task_level)
    assert all(c.parent_key is None for c in task_level)
    assert task_level[0].kind == "assert"
    assert task_level[1].kind == "verify"


def test_check_lines_do_not_disturb_machine_channel():
    """Check lines must be invisible to milestone hashing, drift detection,
    and completion triggers."""
    without = CHECK_PLAN
    for line in list(without.splitlines()):
        if "verify" in line or ("assert" in line and "- [" not in line):
            without = without.replace(line + "\n", "")
    with_snap = parse_plan(CHECK_PLAN)
    without_snap = parse_plan(without)

    assert with_snap.milestone_texts == without_snap.milestone_texts
    assert [i.key for i in with_snap.items] == [i.key for i in without_snap.items]
    assert not milestones_changed(without_snap, with_snap)
    assert new_top_level_completions(without_snap, with_snap) == []
    assert unintended_milestone_edits(without_snap, with_snap) == []


def test_check_items_changed_multiset():
    a = parse_plan("- [ ] G\n  - verify: X\n  - verify: X")
    b = parse_plan("- [ ] G\n  - verify: X")
    c = parse_plan("- [ ] G\n  - verify: X\n  - verify: X")
    assert check_items_changed(a, b)
    assert not check_items_changed(a, c)
    # Status flips never register as check changes
    d = parse_plan("- [x] G\n  - verify: X\n  - verify: X")
    assert not check_items_changed(a, d)


def test_restore_missing_check_items_reanchors_under_parent():
    before = "- [ ] G1\n  - verify: V1\n- [ ] G2\n"
    after = "- [x] G1\n- [ ] G2\n"
    merged = restore_missing_check_items(before, after)
    assert merged is not None
    lines = merged.splitlines()
    assert lines[0] == "- [x] G1"
    assert lines[1] == "  - verify: V1"
    assert lines[2] == "- [ ] G2"
    # Idempotent: nothing missing after the merge
    assert restore_missing_check_items(before, merged) is None


def test_restore_missing_check_items_orphan_becomes_task_level_at_end():
    before = "- [ ] G1\n  - assert: A1\n- [ ] G2\n"
    after = "- [ ] G2\n"  # parent subgoal removed entirely
    merged = restore_missing_check_items(before, after)
    assert merged is not None
    assert merged.splitlines()[-1] == "- assert@end: A1"


def test_restore_missing_check_items_keeps_additions():
    before = "- [ ] G1\n  - verify: V1\n"
    after = "- [ ] G1\n  - verify: V1\n  - verify: V2 (new)\n"
    assert restore_missing_check_items(before, after) is None
    assert not missing_check_items(parse_plan(before), parse_plan(after))


def test_render_plan_grammar_spec_conditional():
    base = render_plan_grammar_spec(include_checks=False)
    extended = render_plan_grammar_spec(include_checks=True)
    # Both-gates-off output carries zero trace of the checking feature
    assert base == PLAN_GRAMMAR_SPEC
    assert "verify" not in base and "assert" not in base
    assert extended.startswith(base)
    assert "- verify:" in extended and "- assert:" in extended and "@end" in extended
    # The capability boundary is declared honestly
    assert "POST-HOC" in extended.upper() or "post-hoc" in extended


def test_grammar_spec_renders_constants():
    assert CONTINUOUS_LOOP_TAG in PLAN_GRAMMAR_SPEC
    assert "[Loop]" in PLAN_GRAMMAR_SPEC
    assert "[/]" in PLAN_GRAMMAR_SPEC and "[x]" in PLAN_GRAMMAR_SPEC


def test_prompts_carry_the_single_sourced_grammar():
    """Drift guard: the prompts that teach the plan format must splice in the
    machine-enforced grammar spec and reference the formal continuous tag,
    and must no longer key loop protection on Exit wording."""
    agents_dir = Path(__file__).resolve().parents[3] / "artemis" / "agents"

    planner_raw = (agents_dir / "planner" / "planner.json").read_text(encoding="utf-8")
    operator_raw = (agents_dir / "operator" / "operator.json").read_text(encoding="utf-8")

    for raw in (planner_raw, operator_raw):
        assert "{{ plan_grammar }}" in raw
        assert "[Loop:continuous]" in raw

    # The old wording-keyed protection contract must not resurface
    planner = json.loads(planner_raw)
    guidelines = planner["blocks"]["validator_guidelines"]
    assert "until manually stopped" not in guidelines
