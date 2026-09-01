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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.agents.checker.checker import (
    CheckReport,
    CheckVerdict,
    _normalize_report,
    assemble_checker_prompt_segments,
    build_checker_tools,
    build_probe_argv,
    run_checkpoint_check,
    run_final_check,
    verdicts_allow_release,
)
from artemis.context import ArtemisContext, ExecutionSetup
from artemis.graph.checkpoints import EvidenceAnchor
from artemis.utils.plan_grammar import CheckItem


def _ci(kind="verify", when="on_complete", text="expected state", parent="p"):
    return CheckItem(kind=kind, when=when, text=text, parent_key=parent)


def _mock_ctx(**setup_kwargs):
    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = ExecutionSetup(**setup_kwargs)
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = "unused"
    ctx.data_engine.get_agent_friendly_steps.return_value = []
    ctx.data_engine.get_step_number.return_value = 7
    return ctx


# --- probe_device: enumerated table, programmatic argv (§8 item 12) ------------------


def test_probe_argv_enumerated_kinds_allowed():
    assert build_probe_argv("alarms") == ["dumpsys", "alarm"]
    assert build_probe_argv("battery") == ["dumpsys", "battery"]
    assert build_probe_argv("foreground") == ["dumpsys", "activity", "activities"]
    assert build_probe_argv("packages") == ["pm", "list", "packages"]
    assert build_probe_argv("setting", {"namespace": "system", "key": "screen_brightness"}) == [
        "settings",
        "get",
        "system",
        "screen_brightness",
    ]
    assert build_probe_argv("content", {"uri": "content://settings/system"}) == [
        "content",
        "query",
        "--uri",
        "content://settings/system",
    ]
    assert build_probe_argv("prop", {"key": "ro.build.version.sdk"}) == [
        "getprop",
        "ro.build.version.sdk",
    ]


def test_probe_argv_rejects_unknown_kind():
    with pytest.raises(ValueError):
        build_probe_argv("shell")
    with pytest.raises(ValueError):
        build_probe_argv("dumpsys")


def test_probe_argv_rejects_whitespace_and_metachars():
    for bad in ("a key", "key;rm", "key|x", "key$", "key`x`", "key&&y", "a\nb"):
        with pytest.raises(ValueError):
            build_probe_argv("prop", {"key": bad})
    with pytest.raises(ValueError):
        build_probe_argv("setting", {"namespace": "system", "key": "a b"})
    with pytest.raises(ValueError):
        build_probe_argv("content", {"uri": "content://a; rm -rf /"})


def test_probe_argv_battery_set_inexpressible():
    """Mutating dumpsys subcommands cannot be expressed: no-parameter probes
    reject every parameter."""
    with pytest.raises(ValueError):
        build_probe_argv("battery", {"extra": "set level 100"})
    with pytest.raises(ValueError):
        build_probe_argv("alarms", {"args": "anything"})
    # And the setting namespace is a closed set
    with pytest.raises(ValueError):
        build_probe_argv("setting", {"namespace": "battery", "key": "level"})


# --- Release decision is node-side and never rewrites verdicts -----------------------


def test_verdicts_allow_release_semantics():
    ok = CheckReport(
        verdicts=[
            CheckVerdict(item_text="a", kind="verify", status="passed", evidence="e"),
            CheckVerdict(item_text="b", kind="verify", status="inconclusive", evidence="e"),
            CheckVerdict(item_text="c", kind="assert", status="failed", evidence="e"),
        ]
    )
    # Assert failures never block release
    assert verdicts_allow_release(ok)

    blocked = CheckReport(
        verdicts=[
            CheckVerdict(item_text="a", kind="verify", status="failed", evidence="e"),
        ]
    )
    assert not verdicts_allow_release(blocked)


def test_normalize_report_downgrades_vague_failures_and_fills_missing():
    items = [_ci(kind="verify", text="v1"), _ci(kind="assert", text="a1")]
    report = CheckReport(
        verdicts=[CheckVerdict(item_text="v1", kind="verify", status="failed", evidence="  ")]
    )
    normalized = _normalize_report(report, items)
    by_text = {(v.kind, v.item_text): v for v in normalized.verdicts}
    # Vague failed -> inconclusive (verdict value hygiene, not release logic)
    assert by_text[("verify", "v1")].status == "inconclusive"
    # Missing item gets an inconclusive verdict, never a silent pass
    assert by_text[("assert", "a1")].status == "inconclusive"


# --- Assembly: prompt segments x entry x content x config (§8 item 18) ---------------


def test_prompt_segments_verify_only_has_no_assert_section():
    prompts = {
        "base_rules": "BASE",
        "verify_semantics": "VERIFY-SEG",
        "assert_semantics": "ASSERT-SEG",
        "anchor_guide": "ANCHOR-SEG",
        "final_guide": "FINAL-SEG",
        "probe_guide": "PROBE-SEG",
    }
    text = assemble_checker_prompt_segments(
        "checkpoint", [_ci(kind="verify")], probe_tool_registered=True, prompts=prompts
    )
    assert "VERIFY-SEG" in text
    assert "ASSERT-SEG" not in text
    assert "ANCHOR-SEG" in text
    assert "FINAL-SEG" not in text
    assert "PROBE-SEG" in text

    text2 = assemble_checker_prompt_segments(
        "final", [_ci(kind="assert")], probe_tool_registered=False, prompts=prompts
    )
    assert "ASSERT-SEG" in text2
    assert "VERIFY-SEG" not in text2
    assert "FINAL-SEG" in text2
    assert "ANCHOR-SEG" not in text2
    # Probe disabled -> zero mention of probing anywhere in the prompt
    assert "PROBE-SEG" not in text2


def test_tool_table_probe_gate():
    ctx = _mock_ctx(disable_device_probes=False)
    names = {t.name for t in build_checker_tools(ctx, "checkpoint")}
    assert "probe_device" in names
    assert "read_note" in names
    assert "get_step_detail" in names
    assert "get_step_screenshot" in names
    # Never any device action, note write, or sub-agent
    assert not names & {
        "save_note",
        "update_note",
        "append_note",
        "click",
        "swipe",
        "ask_diagnoser",
        "ask_explorer",
        "run_adb_command",
    }

    ctx2 = _mock_ctx(disable_device_probes=True)
    names2 = {t.name for t in build_checker_tools(ctx2, "checkpoint")}
    assert "probe_device" not in names2


@pytest.mark.asyncio
async def test_checkpoint_entry_never_touches_live_screen():
    """Evidence discipline: the checkpoint entry must not capture the current
    screen — its evidence is anchored history plus persistent probes."""
    ctx = _mock_ctx()
    report = CheckReport(
        verdicts=[CheckVerdict(item_text="x", kind="verify", status="passed", evidence="e")]
    )

    structured = MagicMock()
    structured.ainvoke = AsyncMock(return_value=report)
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    response = MagicMock()
    response.tool_calls = []

    with (
        patch("artemis.agents.checker.checker.get_llm", return_value=llm),
        patch(
            "artemis.agents.checker.checker.acomplete",
            new=AsyncMock(return_value=response),
        ),
        patch(
            "artemis.agents.checker.checker._capture_final_screen",
            new=AsyncMock(return_value=(None, "SHOULD NOT BE CALLED")),
        ) as capture_mock,
    ):
        result = await run_checkpoint_check(
            ctx,
            check_items=[_ci(text="x")],
            anchor=EvidenceAnchor(anchor_step_id="sid", trigger_ts=0.0, plan_text="- [x] G"),
            goal="the goal",
            subgoal_text="G",
        )

    capture_mock.assert_not_awaited()
    assert result.verdicts[0].status == "passed"


@pytest.mark.asyncio
async def test_final_entry_captures_live_screen():
    ctx = _mock_ctx()
    report = CheckReport(verdicts=[])

    structured = MagicMock()
    structured.ainvoke = AsyncMock(return_value=report)
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    response = MagicMock()
    response.tool_calls = []

    with (
        patch("artemis.agents.checker.checker.get_llm", return_value=llm),
        patch(
            "artemis.agents.checker.checker.acomplete",
            new=AsyncMock(return_value=response),
        ),
        patch(
            "artemis.agents.checker.checker._capture_final_screen",
            new=AsyncMock(return_value=("b64img", "elements")),
        ) as capture_mock,
    ):
        await run_final_check(
            ctx,
            goal="the goal",
            plan_text="- [x] G",
            ledger=[],
            check_items=[_ci(kind="assert", when="at_end", text="no crash", parent=None)],
        )

    capture_mock.assert_awaited_once()
