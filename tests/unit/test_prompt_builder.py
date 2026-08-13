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

"""Unit tests for OperatorPromptBuilder and LoopDetector."""

from artemis.agents.operator.loop_detector import LoopDetector
from artemis.agents.operator.prompt_builder import OperatorPromptBuilder


def test_operator_prompt_builder():
    """Verify prompt rendering formats variables correctly."""
    system_msg = OperatorPromptBuilder.build_system_message()
    assert "ARTEMIS Operator" in system_msg
    assert "click" in system_msg

    human_msg = OperatorPromptBuilder.build_human_message(
        goal="Open Settings",
        sub_goal="Tap Network & internet",
        current_turn=2,
        history="1. Tapped Settings icon",
    )
    assert "Open Settings" in human_msg
    assert "Tap Network & internet" in human_msg
    assert "Current Turn: 2" in human_msg


def test_loop_detector():
    """Verify loop detector flags repeated action sequences."""
    detector = LoopDetector(repetition_threshold=3)
    assert not detector.is_loop_detected()

    # Record distinct actions
    detector.record_action("click", {"target": [100, 200]})
    detector.record_action("swipe", {"action": "up"})
    assert not detector.is_loop_detected()

    # Record 3 identical actions
    detector.record_action("click", {"target": [500, 500]})
    detector.record_action("click", {"target": [500, 500]})
    detector.record_action("click", {"target": [500, 500]})
    assert detector.is_loop_detected()

    # Reset
    detector.reset()
    assert not detector.is_loop_detected()
