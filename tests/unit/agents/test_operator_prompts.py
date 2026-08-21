# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from artemis.agents.operator.prompt_builder import (
    OperatorPromptBuilder,
    load_operator_prompts,
)
from artemis.agents.operator.prompts import apply_operator_prompt_contract


def test_operator_prompt_contract_matches_note_runtime_semantics():
    prompt = apply_operator_prompt_contract(load_operator_prompts()["main_template"])

    assert "write-through tools `update_note` and `append_note`" in prompt
    assert "may accompany at most one Turn-Ending Action" in prompt
    assert "`read_note`, `list_notes`, and `save_note`" in prompt
    assert "Do NOT submit a Turn-Ending Action at the same time" not in prompt
    assert "memory note tools (`read_note`, `list_notes`, `save_note`, `update_note`" not in prompt


def test_troubleshooter_prompt_has_mutually_exclusive_recovery_branches():
    prompt = apply_operator_prompt_contract(load_operator_prompts()["troubleshooter_template"])

    assert "invoke `reply_to_checker` directly" in prompt
    assert "This branch takes precedence over diagnosis" in prompt
    assert "perform that correction directly" in prompt
    assert "also takes precedence over generic diagnosis triggers" in prompt
    assert "A Checker rejection alone is not a mandatory diagnosis trigger" in prompt
    assert "**Validation/Checker Rejection**" not in prompt


def test_operator_prompt_omits_environment_trust_and_explorer_directives():
    for template in load_operator_prompts().values():
        prompt = apply_operator_prompt_contract(template)
        assert "Untrusted Screen Content & Instruction Priority" not in prompt
        assert "Visual Explorer Rule" not in prompt


def test_compatibility_builder_uses_canonical_operator_template():
    expected = apply_operator_prompt_contract(load_operator_prompts()["main_template"])

    assert OperatorPromptBuilder.build_system_message() == expected
    assert "wait_for_delay(seconds=" not in expected
    assert 'press_key(key="home"' not in expected
