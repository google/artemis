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

import base64
import io
from pathlib import Path

from jinja2 import Environment, StrictUndefined, Template
from langchain_core.messages import HumanMessage, SystemMessage
from PIL import Image

from artemis.context import ArtemisContext
from artemis.graph.state import State
from artemis.tools.command_tool import (
    _format_long_output_response,
    _is_output_long,
)
from artemis.utils.logger import get_logger
from artemis.utils.notes import get_note_file_path
from artemis.utils.plan_grammar import parse_plan, render_plan_grammar_spec

logger = get_logger(__name__)


from artemis.agents.prompt_assembly import render_tool_enum, resolve_available
from artemis.mcp.action_specs import OPERATOR_SHELL_ORDER

# --- Assembled tool references -------------------------------------------------------
# The operator.json templates carry availability slots rendered by
# ``apply_operator_prompt_contract``: ``[[ tool_enum(...) ]]`` expands an ordered,
# backticked enumeration limited to the available tool set, and
# ``[% if "x" in available_tools %]...[% endif %]`` gates a self-contained teaching
# segment. An unavailable tool therefore leaves no trace in the prompt at all. The
# slot delimiters are square-bracketed so the standard ``{{ ... }}`` context
# variables (initial_goal, plan_and_history, plan_grammar, ...) pass through
# untouched for the later context render.
#
# The orderings below reproduce the historical prompt wording exactly; with the full
# tool set the rendered output is byte-identical to the pre-assembly literals. See
# artemis/agents/prompt_assembly.py for the assembly rationale.

_PRE_DECISION_HELPER_TOOLS = ("ask_explorer", "ask_diagnoser", "video_analyzer")
_PRE_DECISION_ADB_TOOLS = ("run_adb_command", "manage_task")
_PRE_DECISION_MEMORY_TOOLS = ("read_note", "list_notes", "recall_history")
_PRE_DECISION_ALL_TOOLS = (
    _PRE_DECISION_HELPER_TOOLS + _PRE_DECISION_ADB_TOOLS + _PRE_DECISION_MEMORY_TOOLS
)

# The two device-action enumeration slots in operator.json (identical in both
# templates), in their historical orders. The physical order is the canonical
# manifest's shell-binding order; the turn-ending order is a prompt property.
_PHYSICAL_ACTIONS_ORDER = OPERATOR_SHELL_ORDER
_TURN_ENDING_ORDER = (
    "click",
    "swipe",
    "input_text",
    "long_press",
    "press_key",
    "manage_app",
    "wait_for_delay",
)

#: Tool-loop ceiling per Operator turn (recited in the template, enforced in
#: ``OperatorNode._invoke_llm_loop``).
OPERATOR_MAX_TOOL_ITERATIONS = 20

#: Every tool name the operator prompt slots can reference. ``available_tools=None``
#: resolves to this set, preserving the historical output.
OPERATOR_PROMPT_TOOLSET: frozenset[str] = frozenset(
    _PRE_DECISION_ALL_TOOLS + _PHYSICAL_ACTIONS_ORDER
)

#: Renders the availability slots in operator.json. Square-bracket delimiters keep
#: the standard ``{{ ... }}`` context placeholders inert during this phase.
_CONTRACT_ENV = Environment(
    block_start_string="[%",
    block_end_string="%]",
    variable_start_string="[[",
    variable_end_string="]]",
    comment_start_string="[#",
    comment_end_string="#]",
    keep_trailing_newline=True,
    undefined=StrictUndefined,
)


def apply_operator_prompt_contract(
    prompt_template: str,
    available_tools: frozenset[str] | None = None,
) -> str:
    """Renders a template's availability slots against the actually-available tools.

    Args:
        prompt_template: One of the raw operator.json templates.
        available_tools: Tools that actually exist this run. ``None`` renders with the
            full historical tool set, producing byte-identical output to the
            pre-assembly contract. Unavailable tools disappear from every enumeration
            slot, and instruction segments teaching them are removed wholesale.
    """
    available = frozenset(resolve_available(available_tools, OPERATOR_PROMPT_TOOLSET))

    def tool_enum(names, final_sep: str | None = None) -> str:
        return render_tool_enum(tuple(names), available, final_sep=final_sep)

    return _CONTRACT_ENV.from_string(prompt_template).render(
        available_tools=available,
        tool_enum=tool_enum,
        pre_decision_tools=_PRE_DECISION_ALL_TOOLS,
        helper_tools=_PRE_DECISION_HELPER_TOOLS,
        adb_tools=_PRE_DECISION_ADB_TOOLS,
        memory_tools=_PRE_DECISION_MEMORY_TOOLS,
        physical_actions=_PHYSICAL_ACTIONS_ORDER,
        turn_ending_actions=_TURN_ENDING_ORDER,
    )


class PromptBuilder:
    def __init__(self):
        self.system_parts = []
        self.human_parts = []
        self.human_footer = None

    def add_system_text(self, text: str):
        self.system_parts.append(text)

    def add_human_content(self, content: str | dict):
        self.human_parts.append(content)

    def set_human_footer(self, content: str):
        self.human_footer = content

    def build(self) -> list[SystemMessage | HumanMessage]:
        system_content = "".join(self.system_parts)
        human_content = []
        for p in self.human_parts:
            if isinstance(p, str):
                human_content.append({"type": "text", "text": p})
            else:
                human_content.append(p)

        if self.human_footer:
            human_content.append({"type": "text", "text": self.human_footer})

        return [
            SystemMessage(content=system_content),
            HumanMessage(content=human_content),
        ]


class PromptComponent:
    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        raise NotImplementedError


def resolve_operator_prompt_tools(ctx: ArtemisContext) -> frozenset[str]:
    """The tool set the operator prompt may advertise for this run.

    Extracted verbatim from the template component so the transcript static
    prefix (M2) and the legacy per-turn render assemble against the identical
    availability set.
    """
    # video_analyzer is bound conditionally (graph.py gates it on
    # video_recording_tools_enabled); the prompt must not advertise it when the
    # tool is not actually available this run.
    available = set(OPERATOR_PROMPT_TOOLSET)
    setup = getattr(ctx, "execution_setup", None)
    if not (setup and getattr(setup, "video_recording_tools_enabled", False)):
        available.discard("video_analyzer")

    # recall_history (M4) needs a DataEngine to search and is config-gated;
    # the prompt must not advertise it when the tool is not bound this run.
    if getattr(ctx, "data_engine", None) is None or not _recall_enabled():
        available.discard("recall_history")

    # Device actions the installed actuator backend does not implement disappear
    # from the prompt in lockstep with their tool declarations.
    actuator = getattr(ctx, "actuator", None)
    if actuator is not None and callable(getattr(actuator, "capabilities", None)):
        try:
            from artemis.mcp.action_manifest import (
                DEVICE_ACTIONS,
                available_device_actions,
            )

            available -= DEVICE_ACTIONS - available_device_actions(actuator)
        except Exception as e:
            logger.warning(f"Failed to assemble prompt against actuator: {e}")

    return frozenset(available)


def _operator_grammar_flags(ctx: ArtemisContext) -> tuple[bool, bool]:
    """(include_checks, verification_active) for the template render."""
    setup = getattr(ctx, "execution_setup", None)
    # Grammar spec assembly is a function of configuration: with both check
    # gates disabled, the check-line grammar never enters any prompt.
    include_checks = bool(setup and getattr(setup, "checks_enabled", False))
    # The rejection/finding diagnosis trigger only exists while a mechanism
    # that can produce rejections or findings is active.
    verification_active = include_checks or bool(
        setup and not getattr(setup, "disable_planner_validation", True)
    )
    return include_checks, verification_active


# --- M2 template split -----------------------------------------------------------
# The only volatile span of operator.json's main_template is the plan+history
# section below. The transcript path replaces it with a static pointer so the
# whole system message becomes a byte-stable S region; the legacy path renders
# the untouched template and stays byte-identical.

#: The volatile section of ``main_template`` (must occur exactly once).
PLAN_HISTORY_TEMPLATE_SECTION = "## Current Plan & Execution History\n{{ plan_and_history }}"

#: Static replacement used by the transcript S region.
PLAN_HISTORY_STATIC_POINTER = (
    "## Current Plan & Execution History\n"
    "Provided in the conversation that follows: earlier turns carry the raw"
    " step-by-step execution history (a restored-history block precedes them"
    " after a process restart), and each turn's observation message recites"
    " the current task plan."
)


def render_transcript_static_system(
    prompts: dict,
    ctx: ArtemisContext,
    state: State,
    template_name: str = "main_template",
) -> str:
    """Render the transcript path's byte-stable static system prompt (S region).

    Same template, same availability assembly, and the same render inputs as
    the legacy path — except the volatile plan+history section is swapped for
    a static pointer, so the output never changes across the session.
    """
    prompt_template = prompts.get(template_name)
    if not prompt_template:
        raise KeyError("Failed to format prompt, template not found in operator prompts config.")
    if prompt_template.count(PLAN_HISTORY_TEMPLATE_SECTION) != 1:
        # Hard dependency of the M2 split (redesign §9): without a clean
        # section boundary the S region cannot be byte-stable.
        raise ValueError(
            "operator main_template no longer contains exactly one plan+history"
            " section; the transcript static split cannot be applied."
        )
    static_template = prompt_template.replace(
        PLAN_HISTORY_TEMPLATE_SECTION, PLAN_HISTORY_STATIC_POINTER
    )

    available = resolve_operator_prompt_tools(ctx)
    static_template = apply_operator_prompt_contract(static_template, available_tools=available)
    include_checks, verification_active = _operator_grammar_flags(ctx)
    return Template(static_template).render(
        initial_goal=state.initial_goal,
        subgoals_status="",
        plan_and_history="",
        unified_history="",
        plan_grammar=render_plan_grammar_spec(include_checks),
        verification_active=verification_active,
        checks_active=include_checks,
        transcript_history=True,
        max_burst_actions=_max_burst_actions_for_prompt(),
        max_tool_calls=OPERATOR_MAX_TOOL_ITERATIONS,
    )


def _legacy_elapsed_suffix(ctx: ArtemisContext | None) -> str:
    """`` [T+mm:ss]`` for the legacy observation header (empty without a session clock)."""
    import time

    from artemis.memory.transcript import format_session_offset

    engine = getattr(ctx, "data_engine", None)
    start = getattr(engine, "session_start_time", None)
    if not isinstance(start, (int, float)):
        return ""
    return f" [{format_session_offset(time.time() - start)}]"


class TemplatePromptComponent(PromptComponent):
    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        prompts = kwargs.get("prompts", {})
        template_name = kwargs.get("template_name", "main_template")
        prompt_template = prompts.get(template_name)
        if not prompt_template:
            raise KeyError(
                "Failed to format prompt, template not found in operator prompts config."
            )

        available = resolve_operator_prompt_tools(ctx)

        prompt_template = apply_operator_prompt_contract(prompt_template, available_tools=available)

        plan_and_history = kwargs.get("plan_and_history", "No plan or history yet.")

        include_checks, verification_active = _operator_grammar_flags(ctx)

        full_prompt = Template(prompt_template).render(
            initial_goal=state.initial_goal,
            subgoals_status="",
            plan_and_history=plan_and_history,
            unified_history="",
            plan_grammar=render_plan_grammar_spec(include_checks),
            verification_active=verification_active,
            checks_active=include_checks,
            transcript_history=False,
            max_burst_actions=_max_burst_actions_for_prompt(),
            max_tool_calls=OPERATOR_MAX_TOOL_ITERATIONS,
        )

        parts = full_prompt.split("# CURRENT OBSERVATION")
        builder.add_system_text(parts[0] + f"# CURRENT OBSERVATION{_legacy_elapsed_suffix(ctx)}\n")

        if len(parts) > 1:
            builder.set_human_footer(parts[1])


class ObservationPromptComponent(PromptComponent):
    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        latest_screenshot_b64 = kwargs.get("latest_screenshot_b64")
        minimal_list = kwargs.get("minimal_list")

        builder.add_human_content("--- Current Screenshot ---")
        builder.add_human_content(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{latest_screenshot_b64}"},
            }
        )
        builder.add_human_content(f"--- Visible UI Elements ---\n{minimal_list}")


class PlanRecitationPromptComponent(PromptComponent):
    """Per-turn task-plan recitation for the transcript tail (M2).

    With the plan+history section moved out of the (now static) system
    message, every observation recites the live task plan; the scrub edge
    strips the copies from older turns at depth 1.
    """

    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        from artemis.memory.transcript import PLAN_RECITATION_MARKER

        task_plan = kwargs.get("task_plan") or "No task plan yet."
        builder.add_human_content(f"{PLAN_RECITATION_MARKER}\n{task_plan}")


class FeedbackPromptComponent(PromptComponent):
    """Append-only injection of source-tagged findings.

    Reads ``state.operator_feedback`` (written by ``execution_check_node`` at
    harvest / planner rejection and by exit settlement on a bounce-back).
    Renders nothing when there are no findings — the prompt template itself
    is never switched.
    """

    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        findings = getattr(state, "operator_feedback", None)
        if not findings:
            return
        lines = "\n".join(f"- {f}" for f in findings)
        builder.add_human_content(
            "--- Verification Findings ---\n"
            f"{lines}\n"
            "Each finding is tagged with its source. Checker verdicts"
            " ([verify failed], [final check], [unmet subgoal]) are independent judgments —"
            " address any reverted subgoal accordingly; do not re-litigate"
            " them. [planner] findings are advisory: a lightweight reviewer"
            " had a concern about a plan change that stayed applied — weigh"
            " the reason against your own observations."
        )


#: Header of the execution-incident block in the observation tail.
EXECUTION_INCIDENT_MARKER = "--- Execution Incident (OPEN) ---"


def _max_burst_actions_for_prompt() -> int:
    """The configured fast-action burst ceiling, recited in the operator template."""
    try:
        from artemis.config import load_agent_config

        return int(load_agent_config().pro.execution.max_burst_actions)
    except Exception:
        return 4


def _last_successful_action_before(steps: list, step_number: int | None) -> tuple[str, int] | None:
    """The most recent step before ``step_number`` whose terminal action executed.

    Returns ``(clean action description, step number)`` or None. This is the
    Operator's best candidate for the *trigger* of a transient state: the
    action that summoned the UI the failed target belonged to.
    """
    from artemis.utils.task_tree import format_actions_clean

    candidates = []
    for step in steps or []:
        number = step.get("step_number")
        if not isinstance(number, int):
            continue
        if step_number is not None and number >= step_number:
            continue
        action = step.get("action_taken")
        if not action:
            continue
        result = step.get("last_execution_result")
        if isinstance(result, dict) and result.get("status") not in (None, "success"):
            continue
        candidates.append((number, format_actions_clean(action)))
    if not candidates:
        return None
    number, description = max(candidates, key=lambda c: c[0])
    return description, number


def _incident_target_label(incident: dict) -> str:
    action = incident.get("action") or {}
    normalized = action.get("normalized_coordinates")
    pixels = action.get("coordinates")
    text = action.get("target_text")
    parts = []
    if normalized:
        parts.append(f"normalized {list(normalized)}")
    elif pixels:
        parts.append(f"pixel {list(pixels)}")
    if text:
        parts.append(f'"{text}"')
    return " ".join(parts) if parts else "the recorded target"


def render_execution_incident(incident: dict, steps: list) -> str:
    """The Operator-facing explanation of an open execution incident.

    Structure (deliberately the same every turn so it reads as one continuing
    incident, not a new alarm): facts -> category-specific evidence. The
    response protocol is stated once, in the static system prompt.
    """
    kind = incident.get("kind") or "exec_error"
    category = str(incident.get("category") or "general")
    consecutive = int(incident.get("consecutive_failures") or 1)
    description = incident.get("action_description") or "the planned action"
    reason = str(incident.get("reason") or "").strip()
    burst_size = int(incident.get("burst_size") or 1)
    index = int(incident.get("action_index") or 0)
    evidence = incident.get("evidence") or {}
    step_number = incident.get("step_number")
    target_label = _incident_target_label(incident)
    prior = _last_successful_action_before(steps, step_number)

    lines = [EXECUTION_INCIDENT_MARKER]
    opened = f"Opened at Step {step_number}" if step_number else "Opened last turn"
    lines.append(f"{opened}; consecutive failed turns: {consecutive}.")

    # --- What happened -------------------------------------------------------------
    if burst_size > 1:
        remaining = burst_size - index - 1
        skipped = (
            f" The {remaining} action(s) after it were NOT executed, so the device may"
            " be mid-sequence."
            if remaining > 0
            else ""
        )
        lines.append(
            f"What happened: action {index + 1} of your {burst_size}-action fast burst,"
            f" `{description}`, failed: {reason}.{skipped}"
        )
    elif kind == "safety_net":
        lines.append(
            f"What happened: your planned action `{description}` was NOT executed. The"
            f" pre-execution safety net refused it: {reason}"
        )
    else:
        lines.append(
            f"What happened: your planned action `{description}` was dispatched, but the"
            f" device/executor reported: {reason}"
        )

    # --- Category-specific evidence -------------------------------------------------
    if category == "target_shifted":
        location = evidence.get("new_location")
        bounds = evidence.get("new_bounds")
        where = f" at normalized {location}" if location else ""
        bounds_str = f" (bounds {bounds})" if bounds else ""
        lines.append(
            f"Evidence: the same element still exists but has moved{where}{bounds_str};"
            " the shift exceeded the safety net's auto-correction tolerance."
        )
    elif category == "target_occupied":
        occupant = evidence.get("occupant") or "a different element"
        lines.append(
            f"Evidence: the target position is now covered by {occupant}. Something"
            " appeared on top of your target (dialog, sheet, banner, keyboard, or a"
            " re-laid-out screen)."
        )
    elif category in ("target_disappeared", "pixel_target_disappeared"):
        prior_str = (
            f" Your last successfully executed action was `{prior[0]}` (Step {prior[1]});"
            " if the vanished target belonged to a state that action summoned, that action"
            " is the trigger."
            if prior
            else ""
        )
        lines.append(
            "Evidence: the element you saw in the previous screenshot is no longer on"
            f" screen. Its recorded target was {target_label}.{prior_str}"
        )

    # The response protocol lives in the static system prompt (Execution Incident).
    return "\n".join(lines)


def render_closed_incident(closed: dict) -> str:
    """One-turn notice after an incident closes: settle the original intent."""
    opened = closed.get("step_number")
    closed_at = closed.get("closed_at_step")
    description = closed.get("action_description") or "the blocked action"
    header = (
        f"--- Execution Incident (CLOSED at Step {closed_at}) ---"
        if closed_at
        else "--- Execution Incident (CLOSED) ---"
    )
    opened_str = f" opened at Step {opened}" if opened else ""
    return (
        f"{header}\n"
        f"The incident{opened_str} on `{description}` closed because your last Turn-Ending"
        " Action executed. Settle its original intent against the plan and the live screen:"
        " already served, still pending, or no longer needed."
    )


class ExecutionIncidentPromptComponent(PromptComponent):
    """Renders the open execution incident until a terminal action succeeds,
    then a one-turn CLOSED notice asking the Operator to settle the intent.

    Reads ``state.open_incident`` / ``state.last_closed_incident`` (both written
    by the Validator). The block is its own text part of the observation tail,
    so the transcript ledger keeps it verbatim: the whole resolution effort
    stays legible across turns.
    """

    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        incident = getattr(state, "open_incident", None)
        if isinstance(incident, dict) and incident.get("reason"):
            builder.add_human_content(
                render_execution_incident(incident, kwargs.get("steps") or [])
            )
            return
        closed = getattr(state, "last_closed_incident", None)
        if isinstance(closed, dict) and closed.get("kind"):
            builder.add_human_content(render_closed_incident(closed))


class CheckItemsExplainerPromptComponent(PromptComponent):
    """Behavioral guidance for check lines, rendered iff the CURRENT plan
    actually contains check lines (content-driven, not switch-driven: a resumed
    plan carrying check lines still gets the explanation)."""

    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        if not ctx or not getattr(ctx, "data_engine", None):
            return
        try:
            task_plan_path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")
            if not task_plan_path.exists():
                return
            snapshot = parse_plan(task_plan_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Failed to parse plan for check-items explainer: {e}")
            return
        if not snapshot.all_check_items:
            return
        setup = getattr(ctx, "execution_setup", None)
        max_repairs = getattr(setup, "checkpoint_max_repairs", None)
        if not isinstance(max_repairs, int):
            max_repairs = 2
        builder.add_human_content(
            "--- About the plan's check lines ---\n"
            "The task plan declares `- verify:` / `- assert:` check lines (see Task"
            " Plan Grammar). Use `verify:` lines to confirm your work is complete,"
            " but never declare a check passed yourself or record conclusions on"
            " its behalf. Take NO extra actions for `assert:` lines and never"
            " construct or fake state to satisfy one. Check lines must not be"
            " deleted or reworded (deletions are automatically restored by the"
            " system); keep them verbatim when rewriting the plan — adding new ones"
            " is allowed. Simply mark completions per the plan grammar as usual;"
            " checking runs asynchronously in the background and does not block you."
            f" A failed `verify:` reopens its milestone at most {max_repairs} time(s);"
            " once that repair budget is exhausted, its standing `finding:` line"
            " disappears and the failure stands as the recorded result — do not"
            " keep repairing it."
        )


class BackgroundTasksPromptComponent(PromptComponent):
    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        active_tasks = kwargs.get("active_background_tasks", [])
        if active_tasks:
            lines = [
                "--- Active Background ADB Tasks ---",
            ]
            for task in active_tasks:
                lines.append(
                    f"- TaskId: {task['task_id']}\n  Command:"
                    f" `{task['command']}`\n  Cwd: `{task['cwd']}`\n "
                    f" TerminalID: `{task['terminal_id']}`\n  Accumulated"
                    f" Output: {task['output_line_count']} lines of logs"
                )
            builder.add_human_content("\n".join(lines) + "\n")

        newly_finished_tasks = kwargs.get("newly_finished_tasks", [])
        if newly_finished_tasks:
            lines = [
                "--- NEWLY FINISHED ADB TASKS (Since last step) ---",
            ]
            for task in newly_finished_tasks:
                task_id = task["task_id"]
                command = task["command"]
                status = task["status"]
                output_text = task.get("output_text", "")

                intro = f"- TaskId: {task_id}\n  Command: `{command[:60]}...`\n  Status: {status}"

                if _is_output_long(output_text):
                    formatted = _format_long_output_response(task_id, output_text, intro)
                    # format_long_output_response is multi-line, let's indent it nicely
                    indented = "\n".join(f"  {line}" for line in formatted.splitlines())
                    lines.append(indented)
                else:
                    lines.append(f"{intro}\n  Final Output:\n  {output_text.strip()}")
            builder.add_human_content("\n".join(lines) + "\n")


class TaskPlanWarningPromptComponent(PromptComponent):
    """Suggests a task_plan update after an action turn that did not touch it.

    Only the most recent step is inspected: if it executed a Turn-Ending
    Action and no note tool wrote to ``task_plan``, a soft suggestion is
    injected into the next turn. Pure diagnosis / waiting / observation turns
    legitimately leave the plan alone and get no reminder.
    """

    NOTE_TOOLS = ("update_note", "save_note", "append_note")

    @classmethod
    def _step_updated_task_plan(cls, step: dict) -> bool:
        for tc in step.get("tool_calls", []):
            if tc.get("name") not in cls.NOTE_TOOLS:
                continue
            args = tc.get("args", {})
            note_key = args.get("key") or args.get("name")
            if note_key == "task_plan":
                return True
        return False

    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        steps = kwargs.get("steps", [])
        if not steps:
            return
        last = steps[-1]
        if not last.get("action_taken") or self._step_updated_task_plan(last):
            return
        builder.add_human_content(
            "\nReminder: your last turn executed a Turn-Ending Action without"
            " updating task_plan. If progress was made, record it now with"
            " surgical `update_note` edits: mark what completed and add the"
            " subtasks you are now pursuing."
        )


class ToolLimitWarningPromptComponent(PromptComponent):
    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        if getattr(state, "operator_tool_limit_exceeded", False):
            builder.add_human_content(
                "\nWarning: your last turn used up its tool-call budget without a"
                " Turn-Ending Action. Re-examine the task goal and your plan;"
                " the current approach may not be the right path. If the cause"
                " is unclear, ask_diagnoser can help."
            )


class InjectedInstructionPromptComponent(PromptComponent):
    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        injected = getattr(state, "injected_instruction", None)
        if injected:
            builder.add_human_content(
                f"\n--- User Guidance ---\n"
                f"The user observing your progress has provided the following"
                f" feedback or correction:\n"
                f'"{injected}"\n\n'
                f"Please review this guidance, evaluate it against your current"
                f" screen state and recent history, "
                f"and integrate it into your reasoning. Use it to refine your"
                f" task plan and determine the "
                f"most appropriate next action."
            )


class ScreenshotSimilarityPromptComponent(PromptComponent):
    """Background check: compare current screen against post-action screenshots of the last few steps.

    Inject a note if any are identical. This helps detect if the same screen
    keeps reappearing unexpectedly.
    """

    NUM_STEPS_BACK: int = 3
    MAX_ALLOWED_DIFF_PIXELS: int = 3
    COLOR_TOLERANCE: int = 8

    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        if not ctx or not ctx.data_engine:
            return
        latest_screenshot_b64 = kwargs.get("latest_screenshot_b64")
        steps = kwargs.get("steps") or []
        if not latest_screenshot_b64 or not steps:
            return
        # 1. Load current live image and re-encode to JPEG for symmetric compression matching
        try:
            # Strip data URI header if present
            if "," in latest_screenshot_b64:
                latest_screenshot_b64 = latest_screenshot_b64.split(",", 1)[1]
            curr_img_bytes = base64.b64decode(latest_screenshot_b64)
            raw_img = Image.open(io.BytesIO(curr_img_bytes)).convert("RGB")
            # Symmetric in-memory re-encoding to match JPEG compression artifacts
            jpeg_buf = io.BytesIO()
            raw_img.save(jpeg_buf, format="JPEG", quality=75)
            jpeg_buf.seek(0)
            curr_img = Image.open(jpeg_buf).convert("RGB")
        except Exception:
            return
        # 2. Get last steps with a recorded post_image_name or pre_image_name
        history_steps = [s for s in steps if s.get("post_image_name") or s.get("pre_image_name")]
        recent_steps = history_steps[-self.NUM_STEPS_BACK :]
        matched_step_nums = []
        images_dir = Path(ctx.data_engine.global_base_dir) / "images"
        # 3. Compare current image with each past step's post-action screenshot
        for step_rec in recent_steps:
            step_num = step_rec.get("step_number")
            if step_num is None:
                continue
            image_name = step_rec.get("post_image_name") or step_rec.get("pre_image_name")
            if not image_name:
                image_name = step_rec.get("post_image_name")
            if not image_name:
                continue
            image_path = images_dir / f"{image_name}.jpg"
            if not image_path.exists():
                continue
            try:
                past_img = Image.open(image_path).convert("RGB")

                # Must be same dimensions to compare pixel-by-pixel
                if curr_img.size != past_img.size:
                    continue
                # Count differing pixels
                diff_count = self._count_differing_pixels(
                    curr_img, past_img, max_allowed=self.MAX_ALLOWED_DIFF_PIXELS
                )
                if diff_count <= self.MAX_ALLOWED_DIFF_PIXELS:
                    matched_step_nums.append(str(step_num))
            except Exception:
                continue
        # 4. Inject note if any identical screenshots are found
        if matched_step_nums:
            steps_str = ", ".join(matched_step_nums)
            note_text = f"Note: the screen is unchanged since step {steps_str} (pixel-identical)."
            builder.add_human_content(note_text)

    def _count_differing_pixels(
        self,
        img1: Image.Image,
        img2: Image.Image,
        max_allowed: int | None = None,
    ) -> int:
        """Count pixels that differ between two RGB images.

        Early-exits if diff_count exceeds max_allowed for performance.
        """
        if max_allowed is None:
            max_allowed = self.MAX_ALLOWED_DIFF_PIXELS
        data1 = img1.getdata()
        data2 = img2.getdata()
        diff_count = 0
        tolerance = self.COLOR_TOLERANCE
        for p1, p2 in zip(data1, data2):
            if (
                abs(p1[0] - p2[0]) > tolerance
                or abs(p1[1] - p2[1]) > tolerance
                or abs(p1[2] - p2[2]) > tolerance
            ):
                diff_count += 1
                if diff_count > max_allowed:
                    break
        return diff_count


def _recall_enabled() -> bool:
    """Whether the recall_history tool is enabled by configuration (M4)."""
    try:
        from artemis.config import load_agent_config

        return bool(load_agent_config().memory.recall.enabled)
    except Exception:
        return True


class HistoricalStateHintPromptComponent(PromptComponent):
    """Local historical-state hint from stored perceptual hashes (M4).

    Compares the current screenshot's dHash against the post-action hashes
    stamped on every recorded step (``extra_metadata["post_image_dhash"]``,
    including steps already chunk-compressed out of the visible transcript) —
    an O(n) integer scan, no model call, no historical image bytes. On a
    close match to a step *older* than the recent window it injects a one-line
    hint pointing at ``recall_history``.

    Division of labor with :class:`ScreenshotSimilarityPromptComponent`: that
    component's pixel-exact 3-step look-back covers "stuck on the same
    screen"; this one covers "returned to a much earlier state". When any of
    the last :attr:`RECENT_SILENT_STEPS` steps also matches, this hint stays
    silent so the two never fire on the same regime.
    """

    #: Matches within this many most-recent steps stay silent (the pixel
    #: same-screen note owns that window).
    RECENT_SILENT_STEPS: int = 3
    #: Upper bound on how many historical steps are scanned (most recent first).
    SCAN_CAP: int = 500

    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        latest_screenshot_b64 = kwargs.get("latest_screenshot_b64")
        steps = kwargs.get("steps") or []
        if not latest_screenshot_b64 or len(steps) <= self.RECENT_SILENT_STEPS:
            return

        max_distance = 8
        try:
            from artemis.config import load_agent_config

            transcript_cfg = load_agent_config().memory.transcript
            if not getattr(transcript_cfg, "similarity_hint", True):
                return
            max_distance = int(getattr(transcript_cfg, "similarity_max_distance", 8))
        except Exception as exc:
            logger.debug(
                "Transcript similarity config unavailable; using max_distance=%s: %s",
                max_distance,
                exc,
                exc_info=True,
            )

        try:
            from artemis.utils.image_hash import dhash_hex, hamming_distance_hex

            if "," in latest_screenshot_b64:
                latest_screenshot_b64 = latest_screenshot_b64.split(",", 1)[1]
            current_hash = dhash_hex(base64.b64decode(latest_screenshot_b64))
        except Exception:
            return
        if not current_hash:
            return

        def _step_hash(step: dict) -> str | None:
            meta = step.get("extra_metadata") or {}
            return meta.get("post_image_dhash") or meta.get("pre_image_dhash")

        # Silence rule first: a close match inside the recent window belongs
        # to the pixel-level same-screen note, not this hint.
        for step in steps[-self.RECENT_SILENT_STEPS :]:
            distance = hamming_distance_hex(current_hash, _step_hash(step))
            if distance is not None and distance <= max_distance:
                return

        older_steps = steps[: -self.RECENT_SILENT_STEPS][-self.SCAN_CAP :]
        best_step_number = None
        best_distance = None
        for step in older_steps:
            distance = hamming_distance_hex(current_hash, _step_hash(step))
            if distance is None or distance > max_distance:
                continue
            if best_distance is None or distance <= best_distance:
                # <= keeps the most recent step on ties.
                best_distance = distance
                best_step_number = step.get("step_number")

        if best_step_number is not None:
            builder.add_human_content(
                f"Historical state hint: current screen closely resembles the"
                f" post-action screen from Step {best_step_number}. Use"
                " recall_history only if its details are needed."
            )
