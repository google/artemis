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
_PRE_DECISION_ADB_TOOLS = ("run_adb_command", "manage_task", "analyze_task_output")
_PRE_DECISION_MEMORY_TOOLS = ("read_note", "list_notes", "save_note")
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


class TemplatePromptComponent(PromptComponent):
    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        prompts = kwargs.get("prompts", {})
        template_name = kwargs.get("template_name", "main_template")
        prompt_template = prompts.get(template_name)
        if not prompt_template:
            raise KeyError(
                "Failed to format prompt, template not found in operator prompts config."
            )

        # video_analyzer is bound conditionally (graph.py gates it on
        # video_recording_tools_enabled); the prompt must not advertise it when the
        # tool is not actually available this run.
        available = set(OPERATOR_PROMPT_TOOLSET)
        setup = getattr(ctx, "execution_setup", None)
        if not (setup and getattr(setup, "video_recording_tools_enabled", False)):
            available.discard("video_analyzer")

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

        prompt_template = apply_operator_prompt_contract(
            prompt_template, available_tools=frozenset(available)
        )

        plan_and_history = kwargs.get("plan_and_history", "No plan or history yet.")

        # Grammar spec assembly is a function of configuration: with both check
        # gates disabled, the check-line grammar never enters any prompt.
        include_checks = bool(setup and getattr(setup, "checks_enabled", False))
        # The rejection/finding diagnosis trigger only exists while a mechanism
        # that can produce rejections or findings is active.
        verification_active = include_checks or bool(
            setup and not getattr(setup, "disable_planner_validation", True)
        )

        full_prompt = Template(prompt_template).render(
            initial_goal=state.initial_goal,
            subgoals_status="",
            plan_and_history=plan_and_history,
            unified_history="",
            plan_grammar=render_plan_grammar_spec(include_checks),
            verification_active=verification_active,
        )

        parts = full_prompt.split("# CURRENT OBSERVATION")
        builder.add_system_text(parts[0] + "# CURRENT OBSERVATION\n")

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
            " ([verify failed], [final check]) are independent judgments —"
            " address any reverted subgoal accordingly; do not re-litigate"
            " them. [planner] findings explain why a plan change was rejected"
            " and rolled back — factor the reason into your next strategy."
        )


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
        builder.add_human_content(
            "--- About the plan's check lines ---\n"
            "The task plan declares `- verify:` / `- assert:` check lines."
            " `verify:` lines are the acceptance criteria for their subgoal — use"
            " them to confirm your work is complete, but the judgment is made by"
            " an independent Checker: never declare a check passed yourself or"
            " record conclusions on its behalf. `assert:` lines are test"
            " assertions — take NO extra actions for them and never construct or"
            " fake state to satisfy one; a failing assertion is a legitimate test"
            " result. Check lines must not be deleted or reworded (deletions are"
            " automatically restored by the system); keep them verbatim when"
            " rewriting the plan — adding new ones is allowed. Simply mark"
            " completions per the plan grammar as usual; checking runs"
            " asynchronously in the background and does not block you."
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


class ShortTermMemoryPromptComponent(PromptComponent):
    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        if state.short_term_memory:
            builder.add_human_content(
                f"--- Short-Term Memory (Scratchpad) ---\n{state.short_term_memory}\n"
            )


class TaskPlanWarningPromptComponent(PromptComponent):
    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        steps = kwargs.get("steps", [])
        if len(steps) >= 2:
            last_two_steps = steps[-2:]
            modified_in_last_two = False
            for step in last_two_steps:
                tool_calls = step.get("tool_calls", [])
                for tc in tool_calls:
                    if tc.get("name") in [
                        "update_note",
                        "save_note",
                        "append_note",
                    ]:
                        args = tc.get("args", {})
                        note_key = args.get("key") or args.get("name")
                        if note_key == "task_plan":
                            modified_in_last_two = True
                            break
                if modified_in_last_two:
                    break

            if not modified_in_last_two:
                builder.add_human_content(
                    "\nReminder: You have not updated the task_plan for two"
                    " consecutive turns. Please check the latest progress,"
                    " reflect on whether the task planning is detailed enough,"
                    " and whether every pending item is listed as a subtask."
                    " Please update the completed tasks and pending tasks in"
                    " detail."
                )


class ToolLimitWarningPromptComponent(PromptComponent):
    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        if getattr(state, "operator_tool_limit_exceeded", False):
            builder.add_human_content(
                "\n Warning: You did not execute any screen interaction actions"
                " in your last turn. Please re-examine the task goal and revise"
                " your plan; your current approach may not be the correct path."
                " Actively calling ask_diagnoser can help you diagnose the"
                " issue."
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
            note_text = (
                f"Note: Screenshot in step {steps_str} seem to be identical to"
                " current screen. This could be intended since not all actions"
                " alter screens."
            )
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
