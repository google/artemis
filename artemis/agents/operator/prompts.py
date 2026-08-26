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
import json
from pathlib import Path

from jinja2 import Template
from langchain_core.messages import HumanMessage, SystemMessage
from PIL import Image

from artemis.context import ArtemisContext
from artemis.graph.state import State
from artemis.tools.command_tool import (
    _format_long_output_response,
    _is_output_long,
)
from artemis.utils.logger import get_logger
from artemis.utils.task_tree import get_active_subgoal_hashes

logger = get_logger(__name__)


_LEGACY_TOOL_CALLING_RULE = """- **Tool Calling Iron Rule**: Do NOT submit a Turn-Ending Action at the same time (in the same tool-call list) as a Pre-Decision Exploratory Tool. You must first gather information (e.g., finding coordinates or diagnosing a state), manage ADB background tasks, or update memory notes. You can use pre-decision exploratory tools to complete this task. You will be prompted again with the tool's result, at which point you can output your final physical/turn-ending action."""

_TOOL_CALLING_RULE = """- **Tool Calling Contract**: Tools that return information needed for the decision (`ask_explorer`, `ask_diagnoser`, `video_analyzer`, `run_adb_command`, `manage_task`, `analyze_task_output`, `read_note`, `list_notes`, and `save_note`) are result-dependent Pre-Decision Tools. Do not submit them in the same tool-call list as a Turn-Ending Action; inspect their result first. The write-through tools `update_note` and `append_note` may accompany at most one Turn-Ending Action when their content is based entirely on evidence already available before that action. Never write an action's expected outcome as if it had already been observed."""

_LEGACY_TOOL_PROTOCOL = """1. **Pre-Decision Exploratory Tools**:
   - *What they are*: Helper/Subagent tools (such as `ask_explorer`, `ask_diagnoser`, `video_analyzer`), ADB command tools (`run_adb_command`, `manage_task`), and memory note tools (`read_note`, `list_notes`, `save_note`, `update_note`, `append_note`).
   - *How they work*: These tools gather details or update/read memory. When you invoke these tools, the framework will immediately execute them and return the results, allowing you to continue thinking and make your final decision.
2. **Turn-Ending Actions**:"""

_TOOL_PROTOCOL = """1. **Result-Dependent Pre-Decision Tools**:
   - *What they are*: Helper/Subagent tools (`ask_explorer`, `ask_diagnoser`, `video_analyzer`), ADB/task tools (`run_adb_command`, `manage_task`, `analyze_task_output`), and memory tools whose result must be inspected (`read_note`, `list_notes`, `save_note`).
   - *How they work*: Invoke these without a Turn-Ending Action, inspect the returned result, and then decide.
2. **Write-Through Memory Tools**:
   - `update_note` and `append_note` may run alongside at most one Turn-Ending Action only when recording facts already observed in the current context. If the note content depends on the action's result, wait for the next observation before writing it.
3. **Turn-Ending Actions**:"""

_LEGACY_CHECKER_REJECTION_TRIGGER = """   - **Validation/Checker Rejection**: The verification agent (Checker) rejected your subgoal completion (i.e., you are in troubleshooter mode and this is a retry)."""

_AMBIGUOUS_CHECKER_REJECTION_TRIGGER = """   - **Ambiguous Validation/Checker Rejection**: Use `ask_diagnoser` only when the rejection cause is unclear, the evidence conflicts, or no safe local correction is evident. A Checker rejection alone is not a mandatory diagnosis trigger."""

_LEGACY_TROUBLESHOOTER_CHALLENGE = """   - *Challenge Checker*: If you visually confirm the subgoal's target state is *already achieved*, you **MUST** invoke the `reply_to_checker` tool to state your observation."""

_TROUBLESHOOTER_CHALLENGE = """   - *Challenge Checker*: If current evidence clearly proves the subgoal is already achieved, invoke `reply_to_checker` directly. This branch takes precedence over diagnosis; do not call `ask_diagnoser` first.
   - *Clear Local Correction*: If the rejection cause is explicit and a safe correction is evident from the current screen, perform that correction directly. This branch also takes precedence over generic diagnosis triggers. Use `ask_diagnoser` only for ambiguity, conflicting evidence, or repeated failure."""

_LEGACY_LARGE_LIST_STRATEGY = """- **Large List & Long Text Exploration Strategy (Zero-Miss, Zero-Duplication Single-Pass Principle)**: When traversing large lists, browsing long text bodies, or scanning continuous content feeds, strictly adhere to the single-pass exploration standard:
  1. **Unidirectional Anchor-Based Traversal (Zero-Miss)**: Maintain a consistent, unidirectional scan (e.g., uniform downward scrolling). Before each scroll, identify the bottom-most visible item/text segment as your visual anchor. Calibrate your scroll distance so that this anchor remains visible near the top of the subsequent screen, creating a seamless visual overlap that guarantees zero missed items. Terminate exploration when a definitive boundary is reached (e.g., list bottom reached or no new content appears after scrolling).
  2. **Memory & Plan-Driven Deduplication (Zero-Duplication)**: Systematically log extracted item names, identifiers, or processing states into notes or memory as you progress. After every screen transition, cross-reference visible items against your recorded ledger and strictly interact only with newly surfaced, unrecorded items to eliminate redundant actions on overlapping elements. Proactively maintain subtasks in `task_plan` (e.g., segmenting by batch, category, or alphabetical ranges like `a-d`), tracking exact paths and progress to prevent backtracking.
  3. **Single-Pass Trust & Anti-Oscillation (No Over-Verification)**: Treat recorded notes as verified ground truth. Once a segment is traversed and logged, consider it permanently resolved. Do NOT scroll back-and-forth or perform redundant re-scans for "double-checking". Only perform incremental re-verification if the list has been refreshed or content has mutated, while maintaining strict single-pass handling of new events."""

_LARGE_LIST_STRATEGY = """- **Large List & Long Text Exploration Strategy (Zero-Miss, Zero-Duplication Single-Pass Principle)**: When traversing large lists, browsing long text bodies, or scanning continuous content feeds, strictly adhere to the single-pass exploration standard:
  1. **Unidirectional Anchor-Based Traversal (Zero-Miss)**: Maintain a consistent, unidirectional scan (e.g., uniform downward scrolling). Before each scroll, identify the bottom-most visible item/text segment as your visual anchor and carry that anchor plus the scan direction in short-term memory or the traversal ledger. After the scroll, evaluate this handoff before doing anything unrelated: if the anchor remains visible with new content beyond it, record the new content and continue in the same direction without scrolling back to verify; if the anchor is missing, continuity is uncertain, so perform at most one minimal reverse recovery to re-establish overlap, then resume the original direction. While the traversal's declared exit condition remains unresolved, keep this traversal active and do not advance to another milestone or mark it complete.
  2. **Boundary-Proven Completion**: A single scroll that reveals no new content is not sufficient evidence that the list is complete. For an exhaustive traversal, stop only when an explicit end-of-list indicator is visible, or when one additional successful swipe in the same direction and within the same list container leaves the same final anchor visible with no new items and no loading state. This boundary probe must remain unidirectional; never reverse direction merely to prove completion. If the task declares an earlier exit condition (for example, find one verified match or process N items), that explicit condition may end the traversal without reaching the list boundary.
  3. **Memory & Plan-Driven Deduplication (Zero-Duplication)**: Systematically log extracted item names, identifiers, or processing states into notes or memory as you progress. After every screen transition, cross-reference visible items against your recorded ledger and strictly interact only with newly surfaced, unrecorded items to eliminate redundant actions on overlapping elements. Proactively maintain subtasks in `task_plan` (e.g., segmenting by batch, category, or alphabetical ranges like `a-d`), tracking exact paths and progress to prevent backtracking.
  4. **Single-Pass Trust & Anti-Oscillation (No Over-Verification)**: Treat recorded notes as verified ground truth. Once a segment is traversed with confirmed anchor continuity and logged, consider it permanently resolved. Do NOT scroll back-and-forth or perform redundant re-scans for "double-checking", even if older execution history has been pruned. Resume from the last recorded anchor and direction. Only the single bounded recovery above, or a refreshed/mutated list, permits limited re-verification; otherwise continue the single pass over new content."""


def apply_operator_prompt_contract(prompt_template: str) -> str:
    """Align prompt-level tool and recovery rules with Operator runtime semantics."""
    prompt_template = prompt_template.replace(
        _LEGACY_TOOL_CALLING_RULE,
        _TOOL_CALLING_RULE,
    )
    prompt_template = prompt_template.replace(
        _LEGACY_TOOL_PROTOCOL,
        _TOOL_PROTOCOL,
    )
    prompt_template = prompt_template.replace(
        _LEGACY_CHECKER_REJECTION_TRIGGER,
        _AMBIGUOUS_CHECKER_REJECTION_TRIGGER,
    )
    prompt_template = prompt_template.replace(
        _LEGACY_TROUBLESHOOTER_CHALLENGE,
        _TROUBLESHOOTER_CHALLENGE,
    )
    prompt_template = prompt_template.replace(
        _LEGACY_LARGE_LIST_STRATEGY,
        _LARGE_LIST_STRATEGY,
    )
    return prompt_template


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

        prompt_template = apply_operator_prompt_contract(prompt_template)

        plan_and_history = kwargs.get("plan_and_history", "No plan or history yet.")

        full_prompt = Template(prompt_template).render(
            initial_goal=state.initial_goal,
            subgoals_status="",
            plan_and_history=plan_and_history,
            unified_history="",
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


class CheckerFeedbackPromptComponent(PromptComponent):
    async def __call__(self, builder: PromptBuilder, state: State, ctx: ArtemisContext, **kwargs):
        if not ctx.data_engine:
            return

        notes_dir = Path(ctx.data_engine.base_dir) / "notes"

        subgoal_hash = "default"
        task_plan_path = notes_dir / "task_plan.md"
        if task_plan_path.exists():
            try:
                content = task_plan_path.read_text(encoding="utf-8")

                parent_hash, _ = get_active_subgoal_hashes(content)
                subgoal_hash = parent_hash
            except Exception as e:
                logger.error(f"Failed to parse active subgoal in component: {e}")

        verification_chat_path = notes_dir / f"verification_chat_{subgoal_hash}.json"
        turns = []
        if verification_chat_path.exists():
            try:
                turns = json.loads(verification_chat_path.read_text(encoding="utf-8"))
                logger.info(f"Read verification chat for {subgoal_hash}: {len(turns)} turns")
            except Exception as e:
                logger.error(f"Error reading verification chat: {e}")

        if turns:
            dialogue_lines = []
            for t in turns:
                role = "Operator" if t["role"] == "operator" else "Checker"
                dialogue_lines.append(f"**{role} (Round {t['round']})**:\n{t['content']}")
            checker_feedback = "\n\n".join(dialogue_lines)

            feedback_prompt = f"--- Checker Feedback ---\n{checker_feedback}"

            builder.add_human_content(feedback_prompt)


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
