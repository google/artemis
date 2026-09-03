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

from artemis.agents.operator.prompts import (
    apply_operator_prompt_contract,
    load_operator_prompts,
)


def test_operator_prompt_contract_matches_note_runtime_semantics():
    prompt = apply_operator_prompt_contract(load_operator_prompts()["main_template"])

    assert "**Write-Through Memory Tools** (`save_note`, `update_note`, `append_note`)" in prompt
    assert "May run alongside the turn's Turn-Ending Action (or Fast-Action Burst)" in prompt
    # The two execution tiers and the incident workflow are advertised.
    assert "**Vetted Single Action (default)**" in prompt
    # The burst ceiling is a context variable filled in by the later template render.
    assert (
        "**Fast-Action Burst (time-critical only)**: two to {{ max_burst_actions }}"
        " Turn-Ending Actions" in prompt
    )
    assert "--- Execution Incident (OPEN) ---" in prompt
    assert "Failure Analyzer" not in prompt
    assert (
        "(`read_note`, `list_notes`, `search_history`, `replay_steps`, `get_step_screenshot`)"
        in prompt
    )
    assert "recall_history" not in prompt
    assert "`save_note`, and `search_history`" not in prompt
    assert "Do NOT submit a Turn-Ending Action at the same time" not in prompt
    assert "memory note tools (`read_note`, `list_notes`, `save_note`, `update_note`" not in prompt


def test_operator_prompt_is_single_template_without_checker_dialogue():
    """The prompt is never switched by verification results: main_template is
    the only template, and no checker-dialogue machinery is advertised."""
    prompts = load_operator_prompts()
    assert set(prompts) == {"main_template"}
    prompt = apply_operator_prompt_contract(prompts["main_template"])
    assert "reply_to_checker" not in prompt
    assert "Checker" not in prompt


def test_operator_prompt_omits_environment_trust_and_explorer_directives():
    for template in load_operator_prompts().values():
        prompt = apply_operator_prompt_contract(template)
        assert "Untrusted Screen Content & Instruction Priority" not in prompt
        assert "Visual Explorer Rule" not in prompt


def test_operator_prompt_keeps_large_list_traversal_single_pass_until_boundary():
    for template in load_operator_prompts().values():
        prompt = apply_operator_prompt_contract(template)

        assert "do not advance to another milestone or mark it complete" in prompt
        assert "A single scroll that reveals no new content is not sufficient evidence" in prompt
        assert "one additional successful swipe in the same direction" in prompt
        assert "never reverse direction merely to prove completion" in prompt
        assert "even if older execution history has been pruned" in prompt
        assert "Terminate exploration when a definitive boundary is reached" not in prompt


def test_contract_strips_legacy_tool_literals():
    expected = apply_operator_prompt_contract(load_operator_prompts()["main_template"])

    assert "wait_for_delay(seconds=" not in expected
    assert 'press_key(key="home"' not in expected
