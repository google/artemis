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

from unittest.mock import MagicMock
from artemis.graph.graph import check_plan_mutation_rejections
from artemis.graph.state import State


def test_check_plan_mutation_incomplete_nested():
    content_before = "- [/] Main task\n  - [ ] Subtask 1"
    content_after = "- [x] Main task\n  - [ ] Subtask 1"
    res = check_plan_mutation_rejections(content_before, content_after)
    assert res is not None
    assert "goals in the task plan that have not been marked as completed" in res


def test_check_plan_mutation_delete_continuous_loop():
    content_before = (
        "- [x] Open app\n"
        "- [/] [Loop] Periodically monitor for new emails "
        "(Exit: Continuous monitoring until manually stopped; Interval: every 5 minutes)\n"
        "  - [x] Polling Check #1: Done"
    )
    content_after = (
        "- [x] Open app\n"
        "- [x] Monitor for new emails\n"
        "  - [x] Baseline established\n"
        "  - [x] Polling Check #1: Done"
    )
    res = check_plan_mutation_rejections(content_before, content_after)
    assert res is not None
    assert "cannot delete an active [Loop]" in res


def test_check_plan_mutation_mark_continuous_loop_completed():
    content_before = (
        "- [x] Open app\n"
        "- [/] [Loop] Periodically monitor for new emails "
        "(Exit: Continuous monitoring until manually stopped; Interval: every 5 minutes)\n"
        "  - [x] Polling Check #1: Done"
    )
    content_after = (
        "- [x] Open app\n"
        "- [x] [Loop] Periodically monitor for new emails "
        "(Exit: Continuous monitoring until manually stopped; Interval: every 5 minutes)\n"
        "  - [x] Polling Check #1: Done"
    )
    res = check_plan_mutation_rejections(content_before, content_after)
    assert res is not None
    assert "cannot be unilaterally marked as completed [x]" in res


def test_check_plan_mutation_user_stopped_allowed():
    content_before = (
        "- [x] Open app\n"
        "- [/] [Loop] Periodically monitor for new emails "
        "(Exit: Continuous monitoring until manually stopped; Interval: every 5 minutes)\n"
        "  - [x] Polling Check #1: Done"
    )
    content_after = (
        "- [x] Open app\n"
        "- [x] [Loop] Periodically monitor for new emails "
        "(Exit: Continuous monitoring until manually stopped; Interval: every 5 minutes)\n"
        "  - [x] Polling Check #1: Done"
    )
    state = MagicMock(spec=State)
    state.injected_instruction = "Please stop the task now."
    res = check_plan_mutation_rejections(content_before, content_after, state=state)
    assert res is None


def test_check_plan_mutation_normal_bounded_task():
    content_before = "- [ ] Open Settings\n- [ ] Toggle WiFi"
    content_after = "- [x] Open Settings\n- [x] Toggle WiFi"
    res = check_plan_mutation_rejections(content_before, content_after)
    assert res is None
