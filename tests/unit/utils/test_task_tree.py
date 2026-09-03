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

import hashlib
from artemis.utils.task_tree import (
    format_step_action_result,
    build_plan_and_history,
    get_active_subgoal_hashes,
    get_recent_subgoal_hashes,
)


def test_build_plan_and_history_separated_layout():
    plan = """- [ ] Open Settings app
- [ ] Navigate to System settings"""

    # No steps executed
    output = build_plan_and_history(plan, [], "default")
    assert plan in output
    assert "--- Execution History ---" in output
    assert "No steps executed yet." in output

    # With steps
    steps = [
        {
            "step_id": "step_1",
            "step_number": 1,
            "relative_time": "2.5s",
            "summary": "Launched settings app",
            "operator_raw_thinking": "Thought 1",
            "action_taken": [{"action": "launch_app"}],
            "last_execution_result": {"status": "success"},
        }
    ]
    output = build_plan_and_history(plan, steps, "default", last_n_detailed=1)
    assert plan in output
    assert "--- Execution History ---" in output
    assert "- **Step 1 (Most Recent Step, Start: 2.5s)**" in output
    assert "    - Thought 1" in output
    assert "  * [Planned Action]: Launched app 'None'" in output


def test_build_plan_and_history_granularities():
    plan = """- [x] Open Settings app
- [/] Navigate to System settings
- [ ] Navigate to Languages & input"""

    subgoal_hash_1 = hashlib.md5(b"Open Settings app").hexdigest()
    subgoal_hash_2 = hashlib.md5(b"Navigate to System settings").hexdigest()

    steps = [
        {
            "step_id": "step_1",
            "step_number": 1,
            "relative_time": "2.5s",
            "summary": "Launched settings",
            "operator_raw_thinking": "Thought 1",
            "action_taken": [{"action": "launch_app"}],
            "last_execution_result": {"status": "success"},
            "extra_metadata": {"subgoal_hash": subgoal_hash_1},
        },
        {
            "step_id": "step_2",
            "step_number": 2,
            "relative_time": "8.0s",
            "summary": "Swiped down",
            "operator_raw_thinking": "Thought 2",
            "action_taken": [{"action": "swipe"}],
            "last_execution_result": {"status": "success"},
            "extra_metadata": {"subgoal_hash": subgoal_hash_2},
        },
        {
            "step_id": "step_3",
            "step_number": 3,
            "relative_time": "15.0s",
            "summary": "Clicked System settings",
            "operator_raw_thinking": "Thought 3",
            "action_taken": [{"action": "click"}],
            "last_execution_result": {"status": "success"},
            "extra_metadata": {"subgoal_hash": subgoal_hash_2},
        },
    ]

    # Test last_n_detailed = 0 (high-level analysis context, all summarized)
    output_0 = build_plan_and_history(plan, steps, subgoal_hash_2, last_n_detailed=0)
    assert "- *Step 1 (Start: 2.5s): Launched settings*" in output_0
    assert "- *Step 2 (Start: 8.0s): Swiped down*" in output_0
    assert "- *Step 3 (Start: 15.0s): Clicked System settings*" in output_0
    assert "*Thought*: Thought 3" not in output_0

    # Test last_n_detailed = 1 (normal execution context, only last detailed)
    output_1 = build_plan_and_history(plan, steps, subgoal_hash_2, last_n_detailed=1)
    assert "- *Step 1 (Start: 2.5s): Launched settings*" in output_1
    assert "- *Step 2 (Start: 8.0s): Swiped down*" in output_1
    assert "- **Step 3 (Most Recent Step, Start: 15.0s)**" in output_1
    assert "    - Thought 3" in output_1

    # Test last_n_detailed = 3 (committee context, last 3 detailed)
    output_3 = build_plan_and_history(plan, steps, subgoal_hash_2, last_n_detailed=3)
    assert "- **Step 1 (Start: 2.5s)**" in output_3
    assert "    - Thought 1" in output_3
    assert "- **Step 2 (Start: 8.0s)**" in output_3
    assert "    - Thought 2" in output_3
    assert "- **Step 3 (Most Recent Step, Start: 15.0s)**" in output_3
    assert "    - Thought 3" in output_3

    # Test keep_subgoal_hashes (milestone filtering for checker/diagnoser)
    # Only keep steps for subgoal_hash_2
    output_filter = build_plan_and_history(
        plan, steps, subgoal_hash_2, keep_subgoal_hashes={subgoal_hash_2}
    )
    assert "Launched settings" not in output_filter  # Step 1 is filtered out!
    assert "Swiped down" in output_filter  # Step 2 belongs to subgoal_hash_2, kept!
    assert "    - Thought 3" in output_filter  # Step 3 is the last step, kept detailed!


def test_get_active_subgoal_hashes_fallback():
    # Scenario 1: Normal Active Subgoal (Level 1 [/] active)
    plan_normal = """- [x] Open Settings app
- [/] Navigate to System settings
- [ ] Navigate to Languages & input"""

    h_settings = hashlib.md5(b"Open Settings app").hexdigest()
    h_system = hashlib.md5(b"Navigate to System settings").hexdigest()

    parent, sub = get_active_subgoal_hashes(plan_normal)
    assert parent == h_system
    assert sub is None

    # Scenario 2: Indented Sub-subgoal Active (Level 2 [/] active, consolidated to parent)
    plan_indented = """- [x] Open Settings app
- [/] Navigate to System settings
    - [/] Scroll down to find System settings
- [ ] Navigate to Languages & input"""

    parent, sub = get_active_subgoal_hashes(plan_indented)
    assert parent == h_system
    assert sub is None

    # Scenario 3: Safe Fallback (All pending "[ ]", no active subgoal)
    plan_fallback = """- [ ] Open Settings app
- [ ] Navigate to System settings
- [ ] Navigate to Languages & input"""

    parent, sub = get_active_subgoal_hashes(plan_fallback)
    assert parent == h_settings  # Successfully fell back to the first subgoal's hash!
    assert sub is None

    # Scenario 4: All Completed (All "[x]", no active subgoal)
    plan_all_done = """- [x] Open Settings app
- [x] Navigate to System settings
- [x] Navigate to Languages & input"""

    parent, sub = get_active_subgoal_hashes(plan_all_done)
    assert parent == "default"
    assert sub is None


def test_build_plan_and_history_sliding_window():
    plan = """- [x] Subgoal 1
- [x] Subgoal 2
- [/] Subgoal 3"""

    subgoal_hash_1 = hashlib.md5(b"Subgoal 1").hexdigest()
    subgoal_hash_2 = hashlib.md5(b"Subgoal 2").hexdigest()
    subgoal_hash_3 = hashlib.md5(b"Subgoal 3").hexdigest()

    steps = [
        {
            "step_id": "step_1",
            "step_number": 1,
            "summary": "Step 1 summary",
            "extra_metadata": {"subgoal_hash": subgoal_hash_1},
        },
        {
            "step_id": "step_2",
            "step_number": 2,
            "summary": "Step 2 summary",
            "extra_metadata": {"subgoal_hash": subgoal_hash_1},
        },
        {
            "step_id": "step_3",
            "step_number": 3,
            "summary": "Step 3 summary",
            "extra_metadata": {"subgoal_hash": subgoal_hash_2},
        },
        {
            "step_id": "step_4",
            "step_number": 4,
            "summary": "Step 4 summary",
            "extra_metadata": {"subgoal_hash": subgoal_hash_3},
        },
        {
            "step_id": "step_5",
            "step_number": 5,
            "summary": "Step 5 summary",
            "extra_metadata": {"subgoal_hash": subgoal_hash_3},
        },
        {
            "step_id": "step_6",
            "step_number": 6,
            "summary": "Step 6 summary",
            "extra_metadata": {"subgoal_hash": subgoal_hash_3},
        },
        {
            "step_id": "step_7",
            "step_number": 7,
            "summary": "Step 7 summary",
            "extra_metadata": {"subgoal_hash": subgoal_hash_3},
        },
    ]

    # Case 1: min_summaries = 2
    # subgoal 3 has 4 steps (4, 5, 6, 7). This already exceeds min_summaries=2.
    # So older steps (1, 2, 3) from completed subgoals should be compressed/hidden!
    output_c1 = build_plan_and_history(
        plan, steps, subgoal_hash_3, min_summaries=2, last_n_detailed=0
    )
    assert "Step 1 summary" not in output_c1
    assert "Step 2 summary" not in output_c1
    assert "Step 3 summary" not in output_c1
    assert "Step 4 summary" in output_c1
    assert "Step 5 summary" in output_c1
    assert "Step 6 summary" in output_c1
    assert "Step 7 summary" in output_c1

    # Case 2: min_summaries = 5
    # subgoal 3 has 4 steps (4, 5, 6, 7), which translates to 3 non-last steps.
    # To reach min_summaries=5 non-last steps, the sliding window slides back to keep Step 3 and Step 2 visible!
    # But Step 1 should still be compressed/hidden.
    output_c2 = build_plan_and_history(
        plan, steps, subgoal_hash_3, min_summaries=5, last_n_detailed=0
    )
    assert "Step 1 summary" not in output_c2
    assert "Step 2 summary" in output_c2  # Kept visible by sliding window!
    assert "Step 3 summary" in output_c2  # Kept visible by sliding window!
    assert "Step 4 summary" in output_c2
    assert "Step 5 summary" in output_c2
    assert "Step 6 summary" in output_c2
    assert "Step 7 summary" in output_c2


def test_build_plan_and_history_clean_success_result():
    plan = "- [ ] Subgoal 1"
    steps = [
        {
            "step_id": "step_1",
            "step_number": 1,
            "relative_time": "2.5s",
            "summary": "Swiped screen",
            "action_taken": [{"action": "swipe"}],
            "last_execution_result": {
                "executed_actions": [{"action": "swipe"}],
                "status": "success",
            },
        }
    ]
    output = build_plan_and_history(plan, steps, "default", last_n_detailed=1)

    # The output should contain "status": "success" but NOT the redundant "executed_actions"
    # Success results are stripped under Factual Realism
    assert "* [Validator Execution Result]" not in output
    assert "executed_actions" not in output


def test_build_plan_and_history_with_tool_calls():
    plan = "- [ ] Subgoal 1"
    steps = [
        {
            "step_id": "step_1",
            "step_number": 1,
            "relative_time": "2.5s",
            "summary": "Read note and swiped",
            "action_taken": [{"action": "swipe"}],
            "last_execution_result": {"status": "success"},
            "tool_calls": [
                {
                    "name": "read_note",
                    "payload": {
                        "args": {"key": "other_note"},
                        "result": "- [ ] Milestone",
                    },
                    "status": "success",
                }
            ],
        }
    ]
    output = build_plan_and_history(plan, steps, "default", last_n_detailed=1)

    assert "* [Operator Decision Loop]:" in output
    assert '`read_note({"key": "other_note"})` -> - [ ] Milestone' in output


def test_build_plan_and_history_with_interleaved_events():
    plan = "- [ ] Subgoal 1"
    steps = [
        {
            "step_id": "step_1",
            "step_number": 1,
            "relative_time": "2.5s",
            "summary": "Read note and thought",
            "action_taken": [{"action": "swipe"}],
            "last_execution_result": {"status": "success"},
            "interleaved_events": [
                {"type": "thought", "content": "I want to search for files."},
                {
                    "type": "tool_call",
                    "name": "list_notes",
                    "args": {"dir": "/notes"},
                    "result": ["notes.txt"],
                },
                {"type": "thought", "content": "I see notes.txt, let's read it."},
                {
                    "type": "tool_call",
                    "name": "read_note",
                    "args": {"file": "notes.txt"},
                    "result": "Milestone plan",
                },
            ],
        }
    ]
    output = build_plan_and_history(plan, steps, "default", last_n_detailed=1)

    # It should render interleaved thoughts and tool calls in natural order
    assert "    - I want to search for files." in output
    assert '    - [Tool Call]: `list_notes({"dir": "/notes"})` -> [\'notes.txt\']' in output
    assert "    - I see notes.txt, let's read it." in output
    assert '    - [Tool Call]: `read_note({"file": "notes.txt"})` -> Milestone plan' in output

    # It should use the new section header
    assert "* [Operator Decision Loop]:" in output


def test_build_plan_and_history_concatenates_thoughts():
    plan = "- [ ] Subgoal 1"
    steps = [
        {
            "step_id": "step_1",
            "step_number": 1,
            "relative_time": "2.5s",
            "summary": "Step with multiple thoughts",
            "action_taken": [{"action": "click", "thought": "Action thought"}],
            "operator_raw_thinking": "Raw thought",
            "last_execution_result": {"status": "success"},
            "interleaved_events": [
                {"type": "thought", "content": "Event thought"},
            ],
        }
    ]
    output = build_plan_and_history(plan, steps, "default", last_n_detailed=1)

    # Check that all thoughts are concatenated and visible in the output
    assert "    - Action thought" in output
    assert "    - Event thought" in output
    assert "    - Raw thought" in output


def test_build_plan_and_history_interleaved_decision_loop():
    plan = "- [ ] Subgoal 1"
    steps = [
        {
            "step_id": "step_1",
            "step_number": 1,
            "relative_time": "2.5s",
            "summary": "Completed search",
            "action_taken": [{"action": "click", "target_text": "Search"}],
            "last_execution_result": {"status": "success"},
            "interleaved_events": [
                {"type": "thought", "content": "I should find search icon."},
                {
                    "type": "tool_call",
                    "name": "ask_explorer",
                    "args": {"query": "search icon"},
                    "result": {"center": [500, 600]},
                },
                {
                    "type": "native_thought",
                    "content": "Clicking coordinates [500, 600] now.",
                },
            ],
        }
    ]
    output = build_plan_and_history(plan, steps, "default", last_n_detailed=1)

    assert plan in output
    assert "--- Execution History ---" in output
    assert "- **Step 1 (Most Recent Step, Start: 2.5s)**" in output
    assert "* [Operator Decision Loop]:" in output
    assert "    - I should find search icon." in output
    assert (
        '- [Tool Call]: `ask_explorer({"query": "search icon"})` -> {"center":'
        " [500, 600]}" in output
    )
    assert "    - Clicking coordinates [500, 600] now." in output
    assert "* [Planned Action]: Tapped 'Search' at None" in output
    assert "* [Validator Execution Result]" not in output


def test_build_plan_and_history_safety_net_and_incident():
    plan = "- [ ] Subgoal 1"
    steps = [
        {
            "step_id": "step_1",
            "step_number": 1,
            "relative_time": "5.0s",
            "summary": "Tried to click search",
            "action_taken": [{"action": "click", "target_text": "Search"}],
            "last_execution_result": {
                "status": "failed",
                "burst": False,
                "execution": [
                    {
                        "action": "click",
                        "target_text": "Search",
                        "attempts": [
                            "Pre-execution validation failed: Target button is not visible"
                        ],
                    }
                ],
                "incident": {
                    "kind": "safety_net",
                    "category": "target_disappeared",
                    "reason": "Target button is not visible",
                    "action": {"action": "click", "target_text": "Search"},
                    "action_description": "Tapped 'Search' at None",
                    "action_index": 0,
                    "burst_size": 1,
                    "step_number": 1,
                    "consecutive_failures": 2,
                    "evidence": {},
                },
            },
            "interleaved_events": [
                {"type": "thought", "content": "Let's click search."},
                # Safety net failure
                {
                    "type": "tool_call",
                    "name": "safety_net_validation",
                    "args": {"target": "Search"},
                    "result": [
                        False,
                        "TARGET_DISAPPEARED",
                        "Target button is not visible",
                    ],
                },
            ],
        }
    ]
    output = build_plan_and_history(plan, steps, "default", last_n_detailed=1)

    assert "- **Step 1 (Most Recent Step, Start: 5.0s)**" in output
    assert "* [Operator Decision Loop]:" in output
    assert "    - Let's click search." in output
    assert (
        "* [Planned Action]: Tapped 'Search' at None (Intercepted by"
        " Pre-Execution Safety Net)" in output
    )
    assert "* [Pre-Execution Safety Net]:" in output
    assert (
        "- [Safety Net Check]: (Intercepted by Pre-Execution Safety Net: Target button is"
        " not visible)" in output
    )
    # No repair agent: the incident IS the result line, with its escalation count.
    assert "Failure Analyzer" not in output
    assert (
        "* [Result]: Error: Intercepted by Pre-Execution Safety Net"
        " (target_disappeared, consecutive failure #2) on action `Tapped 'Search' at"
        " None`: Target button is not visible" in output
    )


def test_build_plan_and_history_fast_action_burst():
    plan = "- [ ] Subgoal 1"
    burst = [
        {"action": "click", "coordinates": [500, 900]},
        {"action": "click", "coordinates": [880, 120], "target_text": "Skip"},
        {"action": "press_key", "keycode": "BACK"},
    ]
    steps = [
        {
            "step_id": "step_1",
            "step_number": 1,
            "relative_time": "2.5s",
            "summary": "Woke the control bar and tapped Skip",
            "action_taken": burst,
            "last_execution_result": {
                "status": "failed",
                "burst": True,
                "execution": [
                    {"action": "click", "coordinates": [500, 900]},
                    {
                        "action": "click",
                        "coordinates": [880, 120],
                        "target_text": "Skip",
                        "attempts": ["Error: tap rejected"],
                    },
                    {
                        "action": "press_key",
                        "keycode": "BACK",
                        "attempts": ["Skipped (burst aborted)"],
                    },
                ],
                "incident": {
                    "kind": "exec_error",
                    "category": "general",
                    "reason": "Error: tap rejected",
                    "action": burst[1],
                    "action_description": "Tapped 'Skip' at [880, 120]",
                    "action_index": 1,
                    "burst_size": 3,
                    "step_number": 1,
                    "consecutive_failures": 1,
                    "evidence": {},
                },
            },
        }
    ]
    # Detailed view: every member with its outcome.
    output = build_plan_and_history(plan, steps, "default", last_n_detailed=1)
    assert (
        "* [Planned Fast-Action Burst]: 3 actions fired back to back without the"
        " safety net" in output
    )
    assert "    1. Tapped element at [500, 900] (executed)" in output
    assert "    2. Tapped 'Skip' at [880, 120] (FAILED: Error: tap rejected)" in output
    assert "    3. Pressed key 'BACK' (skipped)" in output
    assert (
        "* [Result]: Error: Execution failed (general, consecutive failure #1) on burst"
        " action 2/3 `Tapped 'Skip' at [880, 120]`: Error: tap rejected; the remaining"
        " burst actions were not executed" in output
    )

    # Compact view: the whole burst on one line.
    compact = format_step_action_result(steps[0])
    assert compact.startswith(
        "Fast-action burst (3 actions, unvetted): Tapped element at [500, 900] ->"
        " Tapped 'Skip' at [880, 120] -> Pressed key 'BACK'"
    )
    assert "(Execution failed: Error: tap rejected)" in compact


def test_get_recent_subgoal_hashes_robust_milestone_exclusion(tmp_path):
    # Scenario:
    # 1. Milestone 1 is completed (hash_completed_1).
    # 2. We had a failed attempt 'hash_failed_old' during Milestone 1.
    # 3. Milestone 2 is completed (hash_completed_2).
    # 4. We are now working on Milestone C (hash_active_c).
    # 5. Milestone C had two previous failed renamed versions: Failed Attempt A and Failed Attempt B.
    # 6. subgoal_hash_chain.json maps: A -> B -> C.

    base_dir = tmp_path
    notes_dir = base_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    chain_path = notes_dir / "subgoal_hash_chain.json"

    hash_completed_1 = hashlib.md5(b"Completed Milestone 1").hexdigest()
    hash_completed_2 = hashlib.md5(b"Completed Milestone 2").hexdigest()
    hash_active_c = hashlib.md5(b"Active Milestone C").hexdigest()

    hash_failed_old = hashlib.md5(b"Failed Attempt Old").hexdigest()
    hash_failed_a = hashlib.md5(b"Failed Attempt A").hexdigest()
    hash_failed_b = hashlib.md5(b"Failed Attempt B").hexdigest()

    # Write the hash chain representing: A -> B -> C
    import json

    chain_data = {hash_failed_a: hash_failed_b, hash_failed_b: hash_active_c}
    chain_path.write_text(json.dumps(chain_data), encoding="utf-8")

    steps = [
        # Steps from Completed Milestone 1
        {"step_id": "s1", "extra_metadata": {"subgoal_hash": hash_completed_1}},
        # Old failed attempt during Milestone 1
        {"step_id": "s2", "extra_metadata": {"subgoal_hash": hash_failed_old}},
        # Steps from Completed Milestone 2 (Most recent completed milestone)
        {"step_id": "s3", "extra_metadata": {"subgoal_hash": hash_completed_2}},
        {"step_id": "s4", "extra_metadata": {"subgoal_hash": hash_completed_2}},
        # Steps from Failed Attempt A (Part of the active task slot's past renames)
        {"step_id": "s5", "extra_metadata": {"subgoal_hash": hash_failed_a}},
        {"step_id": "s6", "extra_metadata": {"subgoal_hash": hash_failed_a}},
        # Steps from Failed Attempt B (Part of the active task slot's past renames)
        {"step_id": "s7", "extra_metadata": {"subgoal_hash": hash_failed_b}},
        {"step_id": "s8", "extra_metadata": {"subgoal_hash": hash_failed_b}},
        # Steps from Active Milestone C
        {"step_id": "s9", "extra_metadata": {"subgoal_hash": hash_active_c}},
    ]

    # Test raw transitive alias resolver
    from artemis.utils.task_tree import get_all_subgoal_aliases

    aliases = get_all_subgoal_aliases(hash_active_c, base_dir)
    assert aliases == {hash_active_c, hash_failed_b, hash_failed_a}

    keep_hashes = get_recent_subgoal_hashes(steps, hash_active_c, base_dir)

    # Should keep:
    # 1. The current active subgoal (hash_active_c)
    assert hash_active_c in keep_hashes

    # 2. The failed/renamed subgoals during active slot (hash_failed_b, hash_failed_a) - resolved transitively via chain
    assert hash_failed_b in keep_hashes
    assert hash_failed_a in keep_hashes

    # 3. The single most recent completed subgoal (hash_completed_2) for continuity context
    assert hash_completed_2 in keep_hashes

    # Should prune:
    # 4. Older completed subgoals (hash_completed_1)
    assert hash_completed_1 not in keep_hashes

    # 5. Older failed subgoals from previously completed milestones (hash_failed_old)
    assert hash_failed_old not in keep_hashes
