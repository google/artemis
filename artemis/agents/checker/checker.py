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

"""Checker: independent, zero-side-effect verdict agent.

Two entry points share one tool loop:

- :func:`run_checkpoint_check` — audits a completed subgoal's ``on_complete``
  check items against the evidence anchored at completion time. Deliberately
  has NO live-screen access: the device has moved on, and the tool table (not
  prompt persuasion) enforces the evidence discipline.
- :func:`run_final_check` — audits the user's original goal plus all declared
  check items at task exit, with the final screen state available.

Both entries only ever read: step history, notes, and an enumerated table of
read-only device probes. No device actions, no note writes, no sub-agents.
The release decision is computed by the caller from the verdicts
(:func:`verdicts_allow_release`) — releasing never rewrites a verdict.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, tool

from pydantic import BaseModel, Field

from artemis.constants import CHECKER_MAX_ITERATIONS
from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace, trace_langchain_tool
from artemis.services.llm import acomplete, get_llm, invoke_llm_with_timeout_message
from artemis.tools.scratchpad import get_read_note_tool_pure
from artemis.tools.tool_wrapper import (
    get_tool_result_content,
    invoke_tool_with_injection,
)
from artemis.utils.logger import get_logger
from artemis.utils.ocr_api import is_ocr_configured, perform_ocr
from artemis.utils.ocr_xml_fusion import (
    _crop_image_remove_status_bar,
    _detect_status_bar_height,
    _map_coordinates_back,
    fuse_ocr_with_xml,
)
from artemis.utils.visualization import format_minimal_list_with_elements

logger = get_logger(__name__)

_PROBE_OUTPUT_LIMIT = 8000


# --- Structured output ----------------------------------------------------------------


class CheckVerdict(BaseModel):
    """One check item's verdict. The verdict value is never altered by the
    release decision (fail-open affects release only)."""

    item_text: str = Field(description="The check line's text, quoted verbatim.")
    kind: Literal["verify", "assert"]
    status: Literal["passed", "failed", "inconclusive"]
    evidence: str = Field(
        description=(
            "Concrete observation backing the verdict: a probe output, note"
            " excerpt, or screenshot element. Vague evidence invalidates a"
            " failed verdict."
        )
    )
    suggestion: str = Field(
        default="",
        description="For failed verify items only: actionable repair hint.",
    )


class CheckReport(BaseModel):
    verdicts: list[CheckVerdict] = Field(default_factory=list)
    unmet_subgoals: list[str] = Field(
        default_factory=list,
        description=(
            "Final check only: plan subgoals (quoted verbatim) marked complete"
            " whose verify criteria are demonstrably unmet."
        ),
    )


def verdicts_allow_release(report: CheckReport) -> bool:
    """Node-side release decision: every verify verdict passed or inconclusive.

    Computed outside the model so that releasing (fail-open) can never rewrite
    a verdict value. Assert failures never block release.
    """
    return all(
        v.status in ("passed", "inconclusive") for v in report.verdicts if v.kind == "verify"
    )


# --- Read-only device probes (enumerated; no free-form command channel) --------------


_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_URI_RE = re.compile(r"^content://[A-Za-z0-9./_-]+$")
_SETTING_NAMESPACES = frozenset({"system", "secure", "global"})


@dataclass(frozen=True)
class ProbeParam:
    name: str
    pattern: re.Pattern
    allowed: frozenset[str] | None = None


@dataclass(frozen=True)
class ProbeSpec:
    """A fixed argv template. ``<name>`` placeholders are substituted with
    regex-validated parameters; argv is built as a list and never passes
    through shell string concatenation."""

    argv: tuple[str, ...]
    params: tuple[ProbeParam, ...] = field(default=())


PROBES: dict[str, ProbeSpec] = {
    "alarms": ProbeSpec(argv=("dumpsys", "alarm")),
    "battery": ProbeSpec(argv=("dumpsys", "battery")),
    "foreground": ProbeSpec(argv=("dumpsys", "activity", "activities")),
    "notifications": ProbeSpec(argv=("dumpsys", "notification")),
    "setting": ProbeSpec(
        argv=("settings", "get", "<namespace>", "<key>"),
        params=(
            ProbeParam("namespace", _KEY_RE, _SETTING_NAMESPACES),
            ProbeParam("key", _KEY_RE),
        ),
    ),
    "content": ProbeSpec(
        argv=("content", "query", "--uri", "<uri>"),
        params=(ProbeParam("uri", _URI_RE),),
    ),
    "packages": ProbeSpec(argv=("pm", "list", "packages")),
    "prop": ProbeSpec(argv=("getprop", "<key>"), params=(ProbeParam("key", _KEY_RE),)),
}


def build_probe_argv(kind: str, params: dict[str, Any] | None = None) -> list[str]:
    """Builds the argv list for an enumerated probe.

    Raises ``ValueError`` for unknown probe kinds, missing/extra parameters, or
    parameter values that fail their regex (whitespace and shell metacharacters
    can never pass). ``dumpsys battery set``-style mutating subcommands are
    structurally inexpressible: the no-parameter probes accept no arguments at
    all.
    """
    spec = PROBES.get(kind)
    if spec is None:
        raise ValueError(f"Unknown probe kind '{kind}'. Allowed: {', '.join(sorted(PROBES))}.")
    params = dict(params or {})
    expected = {p.name for p in spec.params}
    extra = set(params) - expected
    if extra:
        raise ValueError(f"Probe '{kind}' accepts no parameter(s): {sorted(extra)}")
    missing = expected - set(params)
    if missing:
        raise ValueError(f"Probe '{kind}' requires parameter(s): {sorted(missing)}")

    values: dict[str, str] = {}
    for p in spec.params:
        raw = params[p.name]
        if not isinstance(raw, str) or not p.pattern.match(raw):
            raise ValueError(f"Probe '{kind}' parameter '{p.name}' has an invalid value.")
        if p.allowed is not None and raw not in p.allowed:
            raise ValueError(
                f"Probe '{kind}' parameter '{p.name}' must be one of {sorted(p.allowed)}."
            )
        values[p.name] = raw

    argv: list[str] = []
    for token in spec.argv:
        if token.startswith("<") and token.endswith(">"):
            argv.append(values[token[1:-1]])
        else:
            argv.append(token)
    return argv


async def _execute_probe(ctx: ArtemisContext, argv: list[str]) -> str:
    """Executes a validated probe argv over the ADB device channel."""
    try:
        adb_client = ctx.get_adb_client()
        device = adb_client.device(serial=ctx.device.device_id)
        output = await asyncio.to_thread(device.shell, argv)
        text = str(output)
        if len(text) > _PROBE_OUTPUT_LIMIT:
            text = text[:_PROBE_OUTPUT_LIMIT] + "\n... [output truncated]"
        return text
    except Exception as e:
        return f"Error executing probe: {e}"


def get_probe_tool(ctx: ArtemisContext) -> BaseTool:
    @tool
    async def probe_device(kind: str, params: dict | None = None) -> str:
        """Runs one enumerated read-only device state probe.

        kind: one of 'alarms', 'battery', 'foreground', 'notifications',
        'setting' (params: namespace in system|secure|global, key),
        'content' (params: uri, a content:// URI), 'packages',
        'prop' (params: key). Anything else is rejected.
        """
        try:
            argv = build_probe_argv(kind, params)
        except ValueError as e:
            return f"Error: {e}"
        return await _execute_probe(ctx, argv)

    return probe_device


# --- History / step evidence tools ---------------------------------------------------


def get_step_detail_tool(ctx: ArtemisContext) -> BaseTool:
    @tool
    def get_step_detail(step_no: int) -> str:
        """Returns the recorded details of one execution step (action taken,
        summary, metadata) by its step number."""
        if not ctx.data_engine:
            return "Error: no execution history available."
        record = ctx.data_engine.get_step_record(step_no)
        if record is None:
            return f"Error: step {step_no} not found."
        payload = {
            "step_number": record.step_number,
            "timestamp": record.timestamp,
            "summary": record.summary,
            "action_taken": record.action_taken,
            "last_execution_result": record.last_execution_result,
            "extra_metadata": record.extra_metadata,
            "has_pre_screenshot": bool(record.pre_image_name),
            "has_post_screenshot": bool(record.post_image_name),
        }
        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except Exception as e:
            return f"Error serializing step: {e}"

    return get_step_detail


class _ScreenshotRequest:
    """Sentinel result carrying an image to splice into the conversation."""

    def __init__(self, description: str, image_b64: str | None):
        self.description = description
        self.image_b64 = image_b64


def get_step_screenshot_tool(ctx: ArtemisContext) -> BaseTool:
    @tool
    def get_step_screenshot(step_no: int, which: str = "pre") -> str:
        """Attaches the recorded screenshot of a step to the conversation.
        `which` is 'pre' (state observed at the start of the step) or 'post'
        (state after the step's action)."""
        # The real work happens in the loop's interception; this body only
        # documents the contract for schema generation.
        return f"screenshot request: step {step_no} ({which})"

    return get_step_screenshot


def _load_step_screenshot(ctx: ArtemisContext, step_no: int, which: str) -> _ScreenshotRequest:
    if which not in ("pre", "post"):
        return _ScreenshotRequest(f"Error: 'which' must be 'pre' or 'post', got '{which}'.", None)
    if not ctx.data_engine:
        return _ScreenshotRequest("Error: no execution history available.", None)
    path = ctx.data_engine.get_step_image_path(step_no, which)
    if path is None:
        return _ScreenshotRequest(
            f"No {which}-action screenshot recorded for step {step_no}.", None
        )
    try:
        image_b64 = base64.b64encode(Path(path).read_bytes()).decode("utf-8")
    except Exception as e:
        return _ScreenshotRequest(f"Error reading screenshot: {e}", None)
    return _ScreenshotRequest(
        f"Screenshot of step {step_no} ({which}-action) is attached below.", image_b64
    )


# --- Prompt assembly (segments x entry x content x config) ---------------------------


def _load_prompts() -> dict[str, str]:
    prompts_path = Path(__file__).parent / "checker.json"
    try:
        return json.loads(prompts_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed to load checker prompts: {e}")
        return {}


def probes_enabled(ctx: ArtemisContext) -> bool:
    setup = getattr(ctx, "execution_setup", None)
    return not (setup and getattr(setup, "disable_device_probes", False))


def assemble_checker_prompt_segments(
    entry: Literal["checkpoint", "final"],
    check_items: list,
    probe_tool_registered: bool,
    prompts: dict[str, str] | None = None,
) -> str:
    """Assembles the system prompt segments for one run.

    Segment table (assembly discipline: every rule present corresponds to an
    active mechanism, nothing more):
    - base rules: always
    - verify semantics: only when a verify item is under audit
    - assert semantics: only when an assert item is under audit
    - anchor guide: checkpoint entry only
    - final guide: final entry only
    - probe guide: only when the probe tool is actually registered
    """
    prompts = prompts if prompts is not None else _load_prompts()
    segments = [prompts.get("base_rules", "")]
    kinds = {getattr(ci, "kind", None) for ci in check_items}
    if "verify" in kinds:
        segments.append(prompts.get("verify_semantics", ""))
    if "assert" in kinds:
        segments.append(prompts.get("assert_semantics", ""))
    if entry == "checkpoint":
        segments.append(prompts.get("anchor_guide", ""))
    else:
        segments.append(prompts.get("final_guide", ""))
    if probe_tool_registered:
        segments.append(prompts.get("probe_guide", ""))
    return "\n\n".join(s for s in segments if s)


def build_checker_tools(
    ctx: ArtemisContext, entry: Literal["checkpoint", "final"]
) -> list[BaseTool]:
    """The tool table is the single source of Checker authority — all read-only.

    The checkpoint entry deliberately gets NO live-screen capability: its
    evidence is anchored history plus persistent-state probes; the current
    screen has already moved past the audited moment. The final entry receives
    the final screen through its prompt (not a tool), so the table is identical
    for both entries; the difference lives in what the prompt provides.
    Device actions, note writes, and sub-agents are registered for neither.
    """
    tools: list[BaseTool] = [
        get_step_detail_tool(ctx),
        get_step_screenshot_tool(ctx),
        get_read_note_tool_pure(ctx),
    ]
    if probes_enabled(ctx):
        tools.append(get_probe_tool(ctx))
    return tools


# --- History formatting --------------------------------------------------------------


def _format_history(ctx: ArtemisContext, limit: int = 60) -> str:
    if not ctx.data_engine:
        return "No execution history available."
    try:
        steps = ctx.data_engine.get_agent_friendly_steps() or []
    except Exception as e:
        return f"Failed to load execution history: {e}"
    if not steps:
        return "No execution history available."
    lines = []
    for s in steps[-limit:]:
        num = s.get("step_number")
        rel = s.get("relative_time", "")
        summary = s.get("summary") or ""
        action = s.get("action_taken")
        action_str = ""
        if action and not summary:
            try:
                action_str = json.dumps(action, ensure_ascii=False)[:160]
            except Exception:
                action_str = str(action)[:160]
        lines.append(f"- Step {num} ({rel}): {summary or action_str}".rstrip())
    return "\n".join(lines)


def _format_check_items(check_items: list) -> str:
    lines = []
    for ci in check_items:
        lines.append(f"- [{ci.kind}] ({ci.when}) {ci.text}")
    return "\n".join(lines) if lines else "(none)"


# --- Shared tool loop ----------------------------------------------------------------


async def _structured_report(llm, messages) -> CheckReport:
    structured_llm = llm.with_structured_output(CheckReport)
    result = await invoke_llm_with_timeout_message(structured_llm.ainvoke(messages))
    if isinstance(result, CheckReport):
        return result
    if isinstance(result, dict):
        try:
            return CheckReport.model_validate(result)
        except Exception:
            pass
    return CheckReport(verdicts=[])


def _normalize_report(report: CheckReport, check_items: list) -> CheckReport:
    """Node-side hygiene: vague failed verdicts downgrade to inconclusive; every
    expected item gets a verdict (missing ones are inconclusive)."""
    verdicts: list[CheckVerdict] = []
    for v in report.verdicts:
        if v.status == "failed" and not v.evidence.strip():
            verdicts.append(
                CheckVerdict(
                    item_text=v.item_text,
                    kind=v.kind,
                    status="inconclusive",
                    evidence="failed verdict lacked concrete evidence; downgraded",
                    suggestion=v.suggestion,
                )
            )
        else:
            verdicts.append(v)

    covered = {(v.kind, v.item_text) for v in verdicts}
    for ci in check_items:
        if (ci.kind, ci.text) not in covered:
            verdicts.append(
                CheckVerdict(
                    item_text=ci.text,
                    kind=ci.kind,
                    status="inconclusive",
                    evidence="no verdict produced for this item",
                )
            )
    return CheckReport(verdicts=verdicts, unmet_subgoals=list(report.unmet_subgoals))


async def _run_check_loop(
    ctx: ArtemisContext,
    messages: list,
    tools: list[BaseTool],
    check_items: list,
) -> CheckReport:
    llm = get_llm(ctx=ctx, name="checker")
    traced_tools = [trace_langchain_tool(t, ctx) for t in tools]

    max_iterations = (
        getattr(ctx.execution_setup, "checker_max_iterations", CHECKER_MAX_ITERATIONS)
        if ctx.execution_setup
        else CHECKER_MAX_ITERATIONS
    )

    report: CheckReport | None = None
    for i in range(max_iterations):
        if i == max_iterations - 1:
            messages.append(
                HumanMessage(
                    content=(
                        "This is your final iteration; provide your structured verdict report now."
                    )
                )
            )
            report = await _structured_report(llm, messages)
            break

        active_llm = llm.bind_tools(tools=traced_tools)
        response = await invoke_llm_with_timeout_message(acomplete(active_llm, messages))

        if not response.tool_calls:
            report = await _structured_report(llm, messages + [response])
            break

        messages.append(response)
        for tc in response.tool_calls:
            tool_name = tc["name"].split(":")[-1] if ":" in tc["name"] else tc["name"]
            args = dict(tc["args"])
            if tool_name == "get_step_screenshot":
                shot = _load_step_screenshot(
                    ctx, int(args.get("step_no", -1)), str(args.get("which", "pre"))
                )
                messages.append(ToolMessage(tool_call_id=tc["id"], content=shot.description))
                if shot.image_b64:
                    messages.append(
                        HumanMessage(
                            content=[
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/jpeg;base64,{shot.image_b64}"
                                    },
                                }
                            ]
                        )
                    )
                continue

            tool_to_run = next((t for t in traced_tools if t.name == tool_name), None)
            if tool_to_run is None:
                messages.append(
                    ToolMessage(
                        tool_call_id=tc["id"],
                        content=f"Error: Tool {tool_name} not supported",
                        status="error",
                    )
                )
                continue
            try:
                result_obj = await invoke_tool_with_injection(
                    tool=tool_to_run, args=args, tool_call_id=tc["id"]
                )
                result_str = get_tool_result_content(result_obj)
                status = "error" if str(result_str).startswith("Error") else "success"
            except Exception as e:
                result_str = f"Error running tool {tool_name}: {e}"
                status = "error"
            messages.append(ToolMessage(tool_call_id=tc["id"], content=result_str, status=status))

    if report is None:
        report = CheckReport(verdicts=[])
    return _normalize_report(report, check_items)


# --- Entry points --------------------------------------------------------------------


@trace(type="agent", name="checker")
async def run_checkpoint_check(
    ctx: ArtemisContext,
    check_items,
    anchor,
    goal: str,
    subgoal_text: str,
) -> CheckReport:
    """Audits one completed subgoal's on_complete check items.

    Evidence discipline is enforced by the tool table: no live-screen access —
    the anchored history (anchor step's pre screenshot, previous step's post
    state) and persistent-state probes are the only valid sources.
    """
    items = list(check_items)
    logger.info(f"Starting checkpoint check for '{subgoal_text}' ({len(items)} item(s))")
    prompts = _load_prompts()
    tools = build_checker_tools(ctx, "checkpoint")
    probe_registered = any(t.name == "probe_device" for t in tools)
    system_content = "\n\n".join(
        s
        for s in (
            prompts.get("system", "You are the Checker."),
            assemble_checker_prompt_segments("checkpoint", items, probe_registered, prompts),
        )
        if s
    )

    anchor_step_no = None
    if ctx.data_engine and anchor.anchor_step_id:
        try:
            anchor_step_no = ctx.data_engine.get_step_number(anchor.anchor_step_id)
        except Exception:
            anchor_step_no = None

    human_text = (
        f"# User's Original Goal\n{goal}\n\n"
        f"# Audited Subgoal (just marked complete)\n{subgoal_text}\n\n"
        f"# Check Items\n{_format_check_items(items)}\n\n"
        f"# Evidence Anchor\nThe subgoal was marked complete at step"
        f" {anchor_step_no if anchor_step_no is not None else 'unknown'}."
        " The anchor step's pre-action screenshot and the preceding step's"
        " post-action state capture the completion moment.\n\n"
        f"# Task Plan at Completion Time\n{anchor.plan_text}\n\n"
        f"# Execution History (summaries)\n{_format_history(ctx)}"
    )

    messages = [
        SystemMessage(content=system_content),
        HumanMessage(content=human_text),
    ]
    return await _run_check_loop(ctx, messages, tools, items)


async def _capture_final_screen(ctx: ArtemisContext) -> tuple[str | None, str]:
    """Best-effort capture of the final live screen (final entry only)."""
    try:
        from artemis.controllers.unified_controller import UnifiedMobileController

        controller = UnifiedMobileController(ctx)
        device_data = await controller.get_screen_data()
        screenshot_b64 = device_data.base64
        xml_hierarchy = device_data.elements
        width = device_data.width
        height = device_data.height

        ocr_results = []
        if is_ocr_configured():
            try:
                status_bar_height = _detect_status_bar_height(xml_hierarchy, height)
                if status_bar_height > 0:
                    cropped_b64, _, _ = _crop_image_remove_status_bar(
                        screenshot_b64, status_bar_height
                    )
                    raw_ocr = await perform_ocr(cropped_b64)
                    ocr_results = _map_coordinates_back(raw_ocr, status_bar_height)
                else:
                    ocr_results = await perform_ocr(screenshot_b64)
            except Exception as e:
                logger.warning(f"Final check OCR failed, proceeding without OCR: {e}")

        fused_xml = fuse_ocr_with_xml(xml_hierarchy, ocr_results)
        minimal_list, _, _ = format_minimal_list_with_elements(fused_xml, width, height)
        return screenshot_b64, minimal_list
    except Exception as e:
        logger.warning(f"Failed to capture final screen state: {e}")
        return None, "Final screen state unavailable."


def _format_ledger(ledger: list[dict]) -> str:
    if not ledger:
        return "(no checkpoint verdicts were recorded)"
    lines = []
    for r in ledger:
        lines.append(
            f"- [{r.get('kind')}] '{r.get('item_text')}' -> {r.get('status')}"
            f" (attempt {r.get('attempt_id')}): {r.get('evidence', '')}"
        )
    return "\n".join(lines)


@trace(type="agent", name="checker")
async def run_final_check(
    ctx: ArtemisContext,
    goal: str,
    plan_text: str,
    ledger: list[dict],
    check_items,
) -> CheckReport:
    """Final review at task exit: audits the user's original goal and all
    declared check items, with the final screen state available."""
    items = list(check_items)
    logger.info(f"Starting final check ({len(items)} declared item(s))")
    prompts = _load_prompts()
    tools = build_checker_tools(ctx, "final")
    probe_registered = any(t.name == "probe_device" for t in tools)
    system_content = "\n\n".join(
        s
        for s in (
            prompts.get("system", "You are the Checker."),
            assemble_checker_prompt_segments("final", items, probe_registered, prompts),
        )
        if s
    )

    screenshot_b64, minimal_list = await _capture_final_screen(ctx)

    human_text = (
        f"# User's Original Goal\n{goal}\n\n"
        f"# Final Task Plan\n{plan_text or '(no plan)'}\n\n"
        f"# Declared Check Items\n{_format_check_items(items)}\n\n"
        f"# Verdict Ledger (checkpoint results, authoritative for on_complete"
        f" items)\n{_format_ledger(ledger)}\n\n"
        f"# Execution History (summaries)\n{_format_history(ctx)}\n\n"
        f"# Final Screen Elements\n{minimal_list}"
    )

    content: list = [{"type": "text", "text": human_text}]
    if screenshot_b64:
        content.append({"type": "text", "text": "--- Final Screenshot ---"})
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
            }
        )

    messages = [
        SystemMessage(content=system_content),
        HumanMessage(content=content),
    ]
    return await _run_check_loop(ctx, messages, tools, items)
