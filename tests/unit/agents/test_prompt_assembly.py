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

"""Prompt assembly tests: full-set output is byte-stable, absent tools leave no trace."""

from pathlib import Path
import re

from jinja2 import Template
import pytest

from artemis.agents.operator.prompt_builder import load_operator_prompts
from artemis.agents.operator.prompts import (
    OPERATOR_PROMPT_TOOLSET,
    apply_operator_prompt_contract,
)
from artemis.agents.prompt_assembly import gate_segment, render_tool_enum
from artemis.agents.validator.failure_analyzer import (
    FailureAnalysisStrategy,
    FailureAnalyzer,
    PixelTargetDisappearedStrategy,
    TargetDisappearedStrategy,
)
from artemis.agents.validator.validator import ValidationErrorCategory

REPO_ROOT = Path(__file__).resolve().parents[3]

OPERATOR_DEVICE_TOOLS = (
    "click",
    "input_text",
    "swipe",
    "press_key",
    "manage_app",
    "wait_for_delay",
    "long_press",
)


def _tool_refs(text: str, tool: str) -> list[str]:
    """Finds real tool references (backticked or call form), ignoring prose."""
    return re.findall(rf"(`{tool}`|\b{tool}\()", text)


# --- render_tool_enum ----------------------------------------------------------------


def test_enum_plain_list():
    out = render_tool_enum(("a", "b", "c"), {"a", "b", "c"})
    assert out == "`a`, `b`, `c`"


def test_enum_oxford_or_and():
    tools = ("a", "b", "c")
    assert render_tool_enum(tools, set(tools), final_sep="or") == "`a`, `b`, or `c`"
    assert render_tool_enum(tools, set(tools), final_sep="and") == "`a`, `b`, and `c`"


def test_enum_two_items_no_oxford_comma():
    assert render_tool_enum(("a", "b"), {"a", "b"}, final_sep="or") == "`a` or `b`"


def test_enum_single_and_empty():
    assert render_tool_enum(("a", "b"), {"a"}) == "`a`"
    assert render_tool_enum(("a", "b"), set()) == ""


def test_gate_segment():
    assert gate_segment("teach", {"swipe"}, "swipe") == "teach"
    assert gate_segment("teach", set(), "swipe") == ""
    assert gate_segment("teach", {"a"}, "a", "b") == ""


# --- Operator prompt assembly --------------------------------------------------------


def test_full_toolset_is_the_default():
    """``available_tools=None`` must equal the explicit full set, byte for byte."""
    for template in load_operator_prompts().values():
        assert apply_operator_prompt_contract(template) == apply_operator_prompt_contract(
            template, available_tools=OPERATOR_PROMPT_TOOLSET
        )


@pytest.mark.parametrize("tool", OPERATOR_DEVICE_TOOLS + ("video_analyzer", "recall_history"))
def test_removed_tool_leaves_no_reference(tool):
    """The executable definition of 'an absent tool costs the model nothing'."""
    for name, template in load_operator_prompts().items():
        out = apply_operator_prompt_contract(
            template, available_tools=OPERATOR_PROMPT_TOOLSET - {tool}
        )
        assert not _tool_refs(out, tool), f"'{tool}' still referenced in {name}"


def test_removed_swipe_takes_its_teaching_along():
    """Gating removes the drag/slider teaching, not just the backticked name."""
    template = load_operator_prompts()["main_template"]
    out = apply_operator_prompt_contract(
        template, available_tools=OPERATOR_PROMPT_TOOLSET - {"swipe"}
    )
    assert "swipe/drag" not in out
    assert "sliders/SeekBars" not in out
    # The generic gesture-scope sentences survive.
    assert "Action Scope & Interaction Precision" in out


def test_removed_wait_for_delay_keeps_interval_line_coherent():
    """Only the wait_for_delay parenthetical goes; the SOP grammar line stays."""
    template = load_operator_prompts()["main_template"]
    out = apply_operator_prompt_contract(
        template, available_tools=OPERATOR_PROMPT_TOOLSET - {"wait_for_delay"}
    )
    assert "`Interval: <Interval>`: Declared polling cadence." in out
    assert "Loading & Transitions" not in out


def test_reduced_enums_stay_well_formed():
    template = load_operator_prompts()["main_template"]
    out = apply_operator_prompt_contract(
        template,
        available_tools=OPERATOR_PROMPT_TOOLSET - {"manage_app", "wait_for_delay"},
    )
    assert (
        "Physical device actions (`click`, `input_text`, `swipe`, `press_key`,"
        " `long_press`)" in out
    )
    assert (
        "Turn-Ending Action (`click`, `swipe`, `input_text`, `long_press`, or"
        " `press_key`)" in out
    )


def test_full_set_enum_slots_render_verbatim():
    """With every tool present the historical enumeration wording is reproduced."""
    template = load_operator_prompts()["main_template"]
    out = apply_operator_prompt_contract(template)
    assert (
        "Physical device actions (`click`, `input_text`, `swipe`, `press_key`,"
        " `manage_app`, `wait_for_delay`, `long_press`)" in out
    )
    assert (
        "Turn-Ending Action (`click`, `swipe`, `input_text`, `long_press`,"
        " `press_key`, `manage_app`, or `wait_for_delay`)" in out
    )
    assert (
        "(`ask_explorer`, `ask_diagnoser`, `video_analyzer`, `run_adb_command`,"
        " `manage_task`, `analyze_task_output`, `read_note`, `list_notes`,"
        " `save_note`, and `recall_history`)" in out
    )


# --- Flash prompt assembly -----------------------------------------------------------


def _render_flash(available_tools: frozenset[str]) -> str:
    template_text = (REPO_ROOT / "artemis/agents/flash/flash_runner.md").read_text(
        encoding="utf-8"
    )
    return Template(template_text).render(goal="test goal", available_tools=available_tools)


FLASH_FULL = frozenset({"manage_app", "wait_for_delay", "click_sequence", "click", "swipe"})


def test_flash_full_set_keeps_both_segments():
    out = _render_flash(FLASH_FULL)
    assert "**App Launching**" in out
    assert "**Timed Waiting & Transitions**" in out
    assert "{%" not in out


def test_flash_without_manage_app_drops_app_launching():
    out = _render_flash(FLASH_FULL - {"manage_app"})
    assert "**App Launching**" not in out
    assert not _tool_refs(out, "manage_app")
    # The neighbouring numbered rule survives.
    assert "**Screenshot Pruning Awareness**" in out


def test_flash_without_wait_for_delay_drops_timed_waiting():
    out = _render_flash(FLASH_FULL - {"wait_for_delay"})
    assert "**Timed Waiting & Transitions**" not in out
    assert not _tool_refs(out, "wait_for_delay")
    assert "**State Drift & Progression Tolerance**" in out


# --- FailureAnalyzer strategy gating -------------------------------------------------


def _analyzer() -> FailureAnalyzer:
    return FailureAnalyzer.__new__(FailureAnalyzer)  # _select_strategy needs no ctx


def test_strategy_selection_unchanged_without_gating():
    analyzer = _analyzer()
    assert isinstance(
        analyzer._select_strategy(ValidationErrorCategory.TARGET_DISAPPEARED),
        TargetDisappearedStrategy,
    )
    assert isinstance(
        analyzer._select_strategy(ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED),
        PixelTargetDisappearedStrategy,
    )


def test_strategy_falls_back_when_required_tool_missing():
    """Strategy + prompt are one unit: no click_sequence, no disappeared-prompts."""
    analyzer = _analyzer()
    strategy = analyzer._select_strategy(
        ValidationErrorCategory.TARGET_DISAPPEARED,
        available_tools=frozenset({"click", "swipe"}),
    )
    assert type(strategy) is FailureAnalysisStrategy
    assert strategy.get_prompt_template_name() == "failure_analyzer.md"


def test_strategy_kept_when_required_tool_present():
    analyzer = _analyzer()
    strategy = analyzer._select_strategy(
        ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED,
        available_tools=frozenset({"click_sequence"}),
    )
    assert isinstance(strategy, PixelTargetDisappearedStrategy)
