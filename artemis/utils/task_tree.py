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

import ast
import json
import logging
from pathlib import Path
import re
from typing import Any

from artemis.utils.plan_grammar import parse_plan

logger = logging.getLogger(__name__)


def format_action_clean(action_obj) -> str:
    if not action_obj:
        return "No Action"
    if isinstance(action_obj, list):
        if not action_obj:
            return "No Action"
        action_obj = action_obj[0]

    if not isinstance(action_obj, dict):
        return str(action_obj)

    act_type = action_obj.get("action") or action_obj.get("name")
    if not act_type:
        return json.dumps(action_obj, ensure_ascii=False)

    # Flash records the tool call verbatim: the action name at the top level
    # and every argument (app_name, key, text, direction, ...) under ``args``.
    # Read those as fallbacks so both record shapes render the same phrase.
    raw_args = action_obj.get("args")
    if isinstance(raw_args, dict):
        merged = {k: v for k, v in raw_args.items() if k != "action"}
        merged.update({k: v for k, v in action_obj.items() if v is not None and k != "args"})
        merged.setdefault("intent", raw_args.get("action"))
        action_obj = merged

    target_text = action_obj.get("target_text") or action_obj.get("text")
    coords = action_obj.get("coordinates") or action_obj.get("target")
    app_name = (
        action_obj.get("app_name")
        or action_obj.get("package_name")
        or action_obj.get("package")
        or action_obj.get("package_or_bundle_id")
        or action_obj.get("bundle_id")
    )
    keycode = action_obj.get("keycode") or action_obj.get("key")

    if act_type in ("tap", "click", "long_press"):
        label = f"'{target_text}'" if target_text else "element"
        if act_type == "long_press":
            duration = action_obj.get("duration") or action_obj.get("duration_ms")
            duration_str = f" for {duration}ms" if duration else ""
            return f"Long pressed {label} at {coords}{duration_str}"
        else:
            times = action_obj.get("times") or action_obj.get("click_times") or 1
            try:
                times = int(times)
            except (ValueError, TypeError):
                times = 1

            if times == 2:
                return f"Double tapped {label} at {coords}"
            elif times > 2:
                delay = action_obj.get("delay_ms") or action_obj.get("delay")
                delay_str = f" with {delay}ms delay" if delay else ""
                return f"Tapped {label} at {coords} {times} times{delay_str}"
            else:
                return f"Tapped {label} at {coords}"
    elif act_type in ("input_text", "focus_and_input_text"):
        # The field label is the resolved target's text only — the typed text
        # is not the field's name.
        field_text = action_obj.get("target_text")
        label = f"'{field_text}'" if field_text else "field"
        text_val = action_obj.get("text")
        clear_exist = action_obj.get("clear_exist") or action_obj.get("clear_before_input")
        clear_str = " (without clearing)" if clear_exist is False else ""
        return f"Inputted '{text_val}' into {label} at {coords}{clear_str}"
    elif act_type == "swipe":
        direction = action_obj.get("action_direction") or action_obj.get("direction")
        if not direction and isinstance(action_obj.get("gesture"), str):
            # Flash swipe-by-direction records the direction as ``gesture``
            # (or the FA ``action`` word, already mapped to ``intent``).
            direction = action_obj.get("gesture")
        if not direction and isinstance(action_obj.get("intent"), str):
            direction = action_obj.get("intent")
        duration = action_obj.get("duration") or action_obj.get("duration_ms")
        duration_str = f" over {duration}ms" if duration else ""
        if direction:
            return f"Swiped {direction}{duration_str}"

        start_coords = action_obj.get("start_coordinates")
        end_coords = action_obj.get("end_coordinates")
        if (not start_coords or not end_coords) and "coordinates" in action_obj:
            coords_list = action_obj.get("coordinates")
            if isinstance(coords_list, list) and len(coords_list) == 4:
                start_coords = coords_list[:2]
                end_coords = coords_list[2:]
        return f"Swiped from {start_coords} to {end_coords}{duration_str}"
    elif act_type == "press_key":
        return f"Pressed key '{keycode}'"
    elif act_type == "launch_app":
        return f"Launched app '{app_name}'"
    elif act_type == "stop_app":
        return f"Stopped app '{app_name}'"
    elif act_type == "manage_app":
        intent = action_obj.get("intent")
        if intent == "launch":
            return f"Launched app '{app_name}'"
        if intent == "stop":
            return f"Stopped app '{app_name}'"
        return f"Managed app '{app_name}' (action: {intent})"
    elif act_type == "wait_for_delay":
        delay_ms = (
            action_obj.get("delay_ms") or action_obj.get("time_in_ms") or action_obj.get("duration")
        )
        if not delay_ms:
            delay_sec = action_obj.get("delay_seconds") or action_obj.get("delay")
            if delay_sec:
                try:
                    delay_ms = int(float(delay_sec) * 1000)
                except ValueError:
                    delay_ms = f"{delay_sec}s"
        delay_val = (
            f"{delay_ms}ms"
            if isinstance(delay_ms, int) or (isinstance(delay_ms, str) and delay_ms.isdigit())
            else str(delay_ms)
        )
        return f"Waited for {delay_val}"
    elif act_type in ("click_sequence", "tap_sequence"):
        seq = action_obj.get("target") or action_obj.get("sequence") or []
        return f"Tapped sequence of targets: {seq}"
    else:
        return f"Action: {act_type} with args: {json.dumps(action_obj, ensure_ascii=False)}"


def format_actions_clean(actions) -> str:
    """Renders one action or a fast-action burst (a list of 2+ actions)."""
    if isinstance(actions, list):
        if not actions:
            return "No Action"
        if len(actions) > 1:
            steps = " -> ".join(format_action_clean(a) for a in actions)
            return f"Fast-action burst ({len(actions)} actions, unvetted): {steps}"
        return format_action_clean(actions[0])
    return format_action_clean(actions)


def _is_terminal_attempt_failure(attempts) -> bool:
    if not attempts:
        return False
    last = str(attempts[-1])
    return last != "Success" and not last.startswith("Skipped")


def failed_execution_error(result_obj) -> str | None:
    """The error text of the action that failed in an execution report, if any."""
    if not isinstance(result_obj, dict):
        return None
    exec_list = result_obj.get("execution") or result_obj.get("executed_actions") or []
    if isinstance(exec_list, list):
        for entry in exec_list:
            if isinstance(entry, dict) and _is_terminal_attempt_failure(entry.get("attempts")):
                return " | ".join(str(a) for a in entry["attempts"])
        first = exec_list[0] if exec_list and isinstance(exec_list[0], dict) else {}
        if first.get("error"):
            return str(first["error"])
    error = result_obj.get("error") or result_obj.get("error_msg")
    return str(error) if error else None


def format_incident_clean(incident: dict) -> str:
    """One-line rendering of an execution incident for history and result lines."""
    kind = incident.get("kind") or "exec_error"
    category = incident.get("category") or "general"
    consecutive = incident.get("consecutive_failures") or 1
    description = incident.get("action_description") or format_action_clean(incident.get("action"))
    reason = str(incident.get("reason") or "").strip()
    burst_size = int(incident.get("burst_size") or 1)
    index = int(incident.get("action_index") or 0)
    prefix = (
        "Intercepted by Pre-Execution Safety Net" if kind == "safety_net" else "Execution failed"
    )
    where = f"burst action {index + 1}/{burst_size}" if burst_size > 1 else "action"
    tail = ""
    if burst_size > 1 and index < burst_size - 1:
        tail = "; the remaining burst actions were not executed"
    return (
        f"Error: {prefix} ({category}, consecutive failure #{consecutive}) on"
        f" {where} `{description}`: {reason}{tail}"
    )


def format_result_clean(result_obj) -> str | None:
    if not result_obj:
        return None
    if not isinstance(result_obj, dict):
        return str(result_obj)

    incident = result_obj.get("incident")
    if isinstance(incident, dict) and incident.get("reason"):
        return format_incident_clean(incident)

    status = result_obj.get("status")
    error = failed_execution_error(result_obj)
    if status in ("failed", "error"):
        return f"Error: {error}" if error else "Error"
    if error:
        return f"Error: {error}"
    return None


def safe_parse_validation_result(result: Any) -> list:
    if isinstance(result, (list, tuple)):
        return list(result)
    if not isinstance(result, str):
        return []

    # Clean up enum references in string format

    cleaned = re.sub(
        r"<ValidationErrorCategory\.[A-Z_]+:\s*['\"]([^'\"]*)['\"]>",
        r"'\1'",
        result.strip(),
    )

    # Try parsing with ast.literal_eval

    try:
        parsed = ast.literal_eval(cleaned)
        if isinstance(parsed, (list, tuple)):
            return list(parsed)
    except (ValueError, SyntaxError, TypeError, RecursionError):
        pass

    # Fallback to json.loads if literal_eval failed
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, (list, tuple)):
            return list(parsed)
    except ValueError:
        pass

    return []


#: Limit tool-result text in the live context, which is rebuilt every turn.
LIVE_RESULT_CHARS = 120

#: Replay allows longer tool results, but still caps large OCR and hierarchy dumps.
REPLAY_RESULT_CHARS = 6000


def _clamp_text(text: str, limit: int) -> str:
    if limit and len(text) > limit:
        return text[:limit] + "..."
    return text


def format_tool_call_clean(
    name, args, result, *, result_chars: int = LIVE_RESULT_CHARS
) -> str | None:
    ACTION_TOOLS = {
        "click",
        "click_sequence",
        "tap_sequence",
        "swipe",
        "input_text",
        "focus_and_input_text",
        "press_key",
        "long_press",
        "launch_app",
        "manage_app",
        "stop_app",
        "wait_for_delay",
    }

    # Custom rendering for safety net validations
    if name in ("safety_net_validation", "safety_net_pixel_validation"):
        res_list = safe_parse_validation_result(result)
        if len(res_list) >= 3:
            passed, category, detail = res_list[0], res_list[1], res_list[2]
            if passed:
                return "Passed"
            else:
                return f"Failed - {str(detail).strip()}"
        return "Passed" if result else "Failed"

    if name in ("read_note", "save_note", "update_note") and args.get("key") == "task_plan":
        return None

    # Custom rendering for all Action Tools
    if name in ACTION_TOOLS:
        action_name = name

        # Convert arguments to action_obj format with thorough parameter mapping
        action_obj = {
            "action": action_name,
            "intent": args.get("intent") or args.get("action"),
            "target": (args.get("target") or args.get("coordinates") or args.get("sequence")),
            "target_text": (
                args.get("text") or args.get("target_text") or args.get("target_text_or_desc")
            ),
            "text": args.get("text"),
            "clear_exist": (args.get("clear_exist") or args.get("clear_before_input")),
            "clear_before_input": args.get("clear_before_input"),
            "keycode": args.get("keycode") or args.get("key"),
            "key": args.get("key"),
            "app_name": (
                args.get("app_name")
                or args.get("package_name")
                or args.get("package")
                or args.get("package_or_bundle_id")
                or args.get("bundle_id")
            ),
            "delay": (args.get("delay") or args.get("delay_ms") or args.get("time_in_ms")),
            "delay_ms": args.get("delay_ms"),
            "time_in_ms": args.get("time_in_ms"),
            "duration": (args.get("duration") or args.get("duration_ms") or args.get("time_in_ms")),
            "duration_ms": args.get("duration_ms"),
            "times": args.get("times") or args.get("click_times"),
            "click_times": args.get("click_times"),
            "direction": args.get("direction") or args.get("action_direction"),
            "action_direction": args.get("action_direction"),
        }

        if action_name in ("input_text", "focus_and_input_text"):
            # The typed text is not the field's label.
            action_obj["target_text"] = args.get("target_text") or args.get("target_text_or_desc")

        # Handle special FA swipe "action" parameter
        if action_name == "swipe" and "action" in args:
            action_val = args["action"]
            if isinstance(action_val, list) and len(action_val) == 4:
                action_obj["coordinates"] = action_val
            elif isinstance(action_val, str):
                action_obj["action_direction"] = action_val

        # Call format_action_clean to get factual natural language action
        action_clean = format_action_clean(action_obj)

        # Determine if there was an error in the result
        error_detail = None
        if isinstance(result, dict):
            error_detail = result.get("error") or result.get("message")
        elif isinstance(result, (list, tuple)) and result:
            first_val = result[0]
            if isinstance(first_val, str) and any(
                x in first_val.lower() for x in ("error", "failed", "timeout")
            ):
                error_detail = first_val
        elif isinstance(result, str):
            res_lower = result.lower()
            if "error" in res_lower or "failed" in res_lower or "timeout" in res_lower:
                error_detail = result

        if error_detail:
            error_clean = str(error_detail).strip()

            # If the error is formatted as a Python tuple string, e.g. "('Error...', None, None, None)"
            if error_clean.startswith("(") and error_clean.endswith(")"):
                parts = error_clean[1:-1].split(",")
                if parts:
                    first_part = parts[0].strip()
                    if (first_part.startswith("'") and first_part.endswith("'")) or (
                        first_part.startswith('"') and first_part.endswith('"')
                    ):
                        first_part = first_part[1:-1]
                    error_clean = first_part

            for prefix in (
                "Error executing click: ",
                "Error during click: ",
                "Swipe failed: ",
                "Error during swipe: ",
                "Error executing key press: ",
                "Error executing app launch: ",
                "Error launching app: ",
            ):
                if error_clean.startswith(prefix):
                    error_clean = error_clean[len(prefix) :]
            return f"{action_clean} -> (Execution failed: {error_clean})"
        else:
            return action_clean

    clean_args = {k: v for k, v in args.items() if k not in ("state", "tool_call_id")}
    args_str = json.dumps(clean_args, ensure_ascii=False)

    # Clean the result
    res_str = ""
    if isinstance(result, str) and result.startswith("["):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, list):
                result = parsed
        except (json.JSONDecodeError, TypeError):
            pass

    if isinstance(result, list):
        text_blocks = []
        for block in result:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_blocks.append(block.get("text", "").strip())
                elif block.get("type") == "image_url":
                    pass
        if text_blocks:
            res_str = _clamp_text("\n".join(text_blocks), result_chars)
        else:
            res_str = _clamp_text(str(result), result_chars)
    elif isinstance(result, dict):
        error = result.get("error")
        if error:
            res_str = f"Error: {error}"
        else:
            res_str = _clamp_text(json.dumps(result, ensure_ascii=False), result_chars)
    else:
        res_str = _clamp_text(str(result), result_chars)

    return f"`{name}({args_str})` -> {res_str}"


def _detect_interception(interleaved: list, result: Any) -> tuple[bool, str | None]:
    """Whether the pre-execution safety net intercepted the step's action."""
    for event in interleaved or []:
        if event.get("type") == "tool_call" and event.get("name") in (
            "safety_net_validation",
            "safety_net_pixel_validation",
        ):
            res_list = safe_parse_validation_result(event.get("result"))
            if len(res_list) > 0 and not res_list[0]:
                detail = res_list[2] if len(res_list) >= 3 else None
                return True, detail
            break

    # Fallback on the result structure (legacy / non-interleaved steps).
    if isinstance(result, dict):
        incident = result.get("incident")
        if isinstance(incident, dict) and incident.get("kind") == "safety_net":
            return True, incident.get("reason")
        exec_list = result.get("execution") or []
        if exec_list and isinstance(exec_list[0], dict):
            attempts = exec_list[0].get("attempts") or []
            if attempts and any(
                "pre-execution validation" in str(att).lower()
                or "validation failed" in str(att).lower()
                for att in attempts
            ):
                return True, attempts[0]
    return False, None


def format_step_action_result(step: dict) -> str:
    action = step.get("action_taken")
    action_clean = format_actions_clean(action) if action else "No Action"
    result_obj = step.get("last_execution_result")

    intercepted, safety_net_detail = _detect_interception(
        step.get("interleaved_events") or [], result_obj
    )

    output_parts = [action_clean]

    if intercepted:
        detail_msg = f": {str(safety_net_detail).strip()}" if safety_net_detail else ""
        output_parts.append(f"(Intercepted by Pre-Execution Safety Net{detail_msg})")
    elif isinstance(result_obj, dict) and result_obj.get("status") == "failed":
        error_msg = failed_execution_error(result_obj)
        if error_msg:
            output_parts.append(f"(Execution failed: {str(error_msg).strip()})")

    return " -> ".join(output_parts)


def _burst_member_status(result: Any, index: int) -> str:
    """Per-action outcome suffix for one member of a fast-action burst."""
    if not isinstance(result, dict):
        return ""
    exec_list = result.get("execution") or []
    if index >= len(exec_list) or not isinstance(exec_list[index], dict):
        return " (not executed)"
    attempts = exec_list[index].get("attempts") or []
    if not attempts:
        return " (executed)"
    last = str(attempts[-1])
    if last == "Success":
        return " (executed)"
    if last.startswith("Skipped"):
        return " (skipped)"
    return f" (FAILED: {last.strip()})"


def render_step_replay(
    step: dict,
    *,
    result_chars: int = REPLAY_RESULT_CHARS,
    include_summary: bool = True,
) -> str:
    """Render a step's reasoning, tool calls, action, and execution result.

    ``step`` is an agent-friendly record (``DataEngine.get_agent_friendly_step``
    / ``get_agent_friendly_steps``): coordinates already normalized, traces
    already expanded into ``interleaved_events``. Used by ``replay_steps`` and
    the MCP trace inspector. Screenshots are fetched separately.
    """
    return _render_step_detailed(
        step=step,
        relative_time=str(step.get("relative_time") or "N/A"),
        summary=step.get("summary"),
        action=step.get("action_taken"),
        result=step.get("last_execution_result"),
        is_most_recent=False,
        result_chars=result_chars,
        include_summary=include_summary,
    )


def _screen_description_line(step: dict, summary: Any) -> str | None:
    """The replay's ``[Screen]`` line: the step's stored screen description
    (the visual-transition summary), or its status when no text exists yet.

    ``extra_metadata.summary_status`` is ``pending`` while the background
    summarizer has not finished, ``failed`` when it gave up; without a status
    the line is omitted altogether.
    """
    if summary and str(summary).strip():
        return f"[Screen]: {str(summary).strip()}"
    status = str((step.get("extra_metadata") or {}).get("summary_status") or "").lower()
    if status == "pending":
        return "[Screen]: (screen description pending)"
    if status in ("failed", "unavailable"):
        return "[Screen]: (screen description unavailable)"
    return None


def _render_step_detailed(
    step: dict,
    relative_time: str,
    summary: str,
    action: Any,
    result: Any,
    is_most_recent: bool,
    *,
    result_chars: int = LIVE_RESULT_CHARS,
    include_summary: bool = False,
) -> str:
    # 1. Step Header
    status_str = "Most Recent Step, " if is_most_recent else ""

    # In the live window the summary is omitted from the detailed view header
    # as it is already fully detailed below; replay callers opt in because the
    # summary is the Operator's own claim about the step, which an auditor
    # compares against the evidence that follows.
    step_line = f"- **Step {step['step_number']} ({status_str}Start: {relative_time})**"
    if include_summary:
        screen_line = _screen_description_line(step, summary)
        if screen_line:
            step_line += f"\n  * {screen_line}"

    interleaved = step.get("interleaved_events") or []

    # 2. Determine if Safety Net validation failed (intercepted)
    intercepted, safety_net_detail = _detect_interception(interleaved, result)

    # 3. Collect the Operator's own events (system guardians are rendered separately)
    operator_events = []

    INTERNAL_SYSTEM_TOOLS = {
        "safety_net_validation",
        "safety_net_pixel_validation",
        "hopper",
    }

    if interleaved:
        for event in interleaved:
            e_type = event.get("type")
            name = event.get("name") or ""
            if e_type in ("thought", "native_thought"):
                operator_events.append(event)
            elif (
                e_type == "tool_call"
                and name not in INTERNAL_SYSTEM_TOOLS
                and not name.startswith("_exec_")
            ):
                operator_events.append(event)
    else:
        # Fallback for legacy/non-interleaved steps
        raw_thinking = step.get("operator_raw_thinking")
        native_thinking = step.get("operator_native_thinking")
        if native_thinking:
            operator_events.append({"type": "native_thought", "content": native_thinking})
        if raw_thinking:
            operator_events.append({"type": "thought", "content": raw_thinking})

        legacy_tool_calls = step.get("tool_calls") or []
        for tc in legacy_tool_calls:
            name = tc.get("name") or ""
            if name in INTERNAL_SYSTEM_TOOLS or name.startswith("_exec_"):
                continue
            payload = tc.get("payload") or {}
            tc_args = payload.get("args") or tc.get("args") or {}
            tc_result = (
                payload.get("result") or payload.get("error") or tc.get("result") or "No result"
            )
            operator_events.append(
                {
                    "type": "tool_call",
                    "name": name,
                    "args": tc_args,
                    "result": tc_result,
                }
            )

    # Helper to check if a thought content is already in operator_events
    def thought_exists(content):
        content_clean = content.strip().lower()
        for ev in operator_events:
            if (
                ev.get("type") in ("thought", "native_thought")
                and ev.get("content", "").strip().lower() == content_clean
            ):
                return True
        return False

    # Extract thought from final physical action if present
    action_thought = None
    if action:
        action_parsed = action[0] if isinstance(action, list) and action else action
        if isinstance(action_parsed, dict):
            action_thought = action_parsed.get("thought")
    if action_thought and action_thought.strip() and not thought_exists(action_thought):
        operator_events.insert(0, {"type": "thought", "content": action_thought.strip()})

    # Extract native thinking from LLM thinking block first if not already present
    native_thinking = step.get("operator_native_thinking")
    if native_thinking and native_thinking.strip() and not thought_exists(native_thinking):
        operator_events.append({"type": "native_thought", "content": native_thinking.strip()})

    # Extract raw thinking from LLM text content second if not already present
    raw_thinking = step.get("operator_raw_thinking")
    if raw_thinking and raw_thinking.strip() and not thought_exists(raw_thinking):
        operator_events.append({"type": "thought", "content": raw_thinking.strip()})

    # 4. Render Operator Loop
    if operator_events:
        step_line += "\n  * [Operator Decision Loop]:"
        ACTION_TOOLS = {
            "click",
            "swipe",
            "input_text",
            "press_key",
            "long_press",
            "launch_app",
            "wait_for_delay",
        }
        for event in operator_events:
            e_type = event["type"]
            content = event.get("content") or ""
            name = event.get("name") or ""
            if not content.strip() and e_type != "tool_call":
                continue
            if e_type in ("native_thought", "thought"):
                cleaned = re.sub(
                    r"<short_term_memory>.*?</short_term_memory>",
                    "",
                    content,
                    flags=re.DOTALL,
                )
                cleaned = re.sub(r"</?thought>", "", cleaned, flags=re.DOTALL).strip()
                if cleaned:
                    step_line += f"\n    - {cleaned}"
            elif e_type == "tool_call":
                if name in ACTION_TOOLS:
                    continue
                formatted = format_tool_call_clean(
                    name,
                    event.get("args") or {},
                    event.get("result"),
                    result_chars=result_chars,
                )
                if formatted:
                    step_line += f"\n    - [Tool Call]: {formatted}"

    # Check if the execution failed
    exec_error = None
    if isinstance(result, dict) and result.get("status") in ("failed", "error"):
        exec_error = failed_execution_error(result)

    # 5. Render Planned Action (single vetted action or fast-action burst)
    if action:
        actions = action if isinstance(action, list) else [action]
        if len(actions) > 1:
            step_line += (
                f"\n  * [Planned Fast-Action Burst]: {len(actions)} actions fired back to"
                " back without the safety net"
            )
            for i, member in enumerate(actions):
                step_line += (
                    f"\n    {i + 1}. {format_action_clean(member)}{_burst_member_status(result, i)}"
                )
        else:
            action_clean = format_action_clean(actions[0])
            if intercepted:
                step_line += (
                    f"\n  * [Planned Action]: {action_clean}"
                    " (Intercepted by Pre-Execution Safety Net)"
                )
            elif exec_error:
                step_line += (
                    f"\n  * [Planned Action]: {action_clean} -> (Execution failed: {exec_error})"
                )
            else:
                step_line += f"\n  * [Planned Action]: {action_clean}"

    # 6. Render Pre-Execution Safety Net
    if intercepted:
        step_line += "\n  * [Pre-Execution Safety Net]:"
        detail_str = (
            f"(Intercepted by Pre-Execution Safety Net: {str(safety_net_detail).strip()})"
            if safety_net_detail
            else "(Intercepted by Pre-Execution Safety Net)"
        )
        step_line += f"\n    - [Safety Net Check]: {detail_str}"

    # 7. Validator Execution Result (an open incident renders as the result line)
    if result:
        result_clean = format_result_clean(result)
        if result_clean:
            step_line += f"\n  * [Result]: {result_clean}"

    return step_line


def build_plan_and_history(
    task_plan: str,
    steps: list,
    current_subgoal_hash: str,
    keep_subgoal_hashes: set | None = None,
    min_summaries: int = 5,
    last_n_detailed: int = 1,
    all_detailed: bool = False,
    strict_milestone_pruning: bool = False,
    recent_window_size: int = 3,
    chunks: list | None = None,
) -> str:
    """Builds a clean plan list and separate flat chronological execution history with adaptive compression.

    ``chunks`` (optional, M4): pre-rendered history-chunk blocks — each a dict
    with ``start_step_number``/``end_step_number``/``text``. When provided,
    the chunk blocks are emitted right after the history header and the
    per-step lines inside any chunked range are dropped (the chunk block
    replaces them), except steps that must render detailed (the recent
    window / the most-recent step stay untouched). When ``chunks`` is
    ``None`` or empty the output is byte-identical to the pre-M4 rendering.
    """
    # 1. Plan Checklist Section
    output_parts = []

    # Check if task_plan has actual subgoals (single-source checkbox grammar).
    has_subgoals = bool(task_plan) and bool(parse_plan(task_plan).items)

    if has_subgoals:
        output_parts.append("--- Task Plan ---")
        output_parts.append(task_plan)
        output_parts.append("")
    else:
        # Print the plan fallback if not empty
        if task_plan and task_plan != "No task plan yet.":
            output_parts.extend(
                [
                    "--- Task Plan ---",
                    task_plan,
                    "",
                ]
            )

    output_parts.append("--- Execution History ---")

    # 2. Chronological Steps History Section
    if not steps:
        output_parts.append("No steps executed yet.")
    else:
        full_info_step_id = steps[-1]["step_id"]
        visible_step_ids = set()
        visible_step_ids.add(full_info_step_id)

        # Calculate which steps are visible using milestone filtering or sliding window compression
        if keep_subgoal_hashes is not None:
            # Milestone-specific filtering (checker / diagnoser)
            for step in steps:
                meta = step.get("extra_metadata") or {}
                step_subgoal_hash = meta.get("subgoal_hash")
                if step_subgoal_hash in keep_subgoal_hashes:
                    visible_step_ids.add(step["step_id"])
        else:
            # Default sliding window compression or strict milestone pruning
            if strict_milestone_pruning:
                recent_step_ids = set()
                actual_recent_n = min(len(steps), recent_window_size)
                for step in steps[-actual_recent_n:]:
                    recent_step_ids.add(step["step_id"])

                for i in range(len(steps) - 1, -1, -1):
                    step = steps[i]
                    step_id = step["step_id"]
                    if step_id == full_info_step_id:
                        continue

                    meta = step.get("extra_metadata") or {}
                    step_subgoal_hash = meta.get("subgoal_hash")

                    # Strict pruning: only keep if active subgoal OR in recent continuity window
                    if step_subgoal_hash == current_subgoal_hash or step_id in recent_step_ids:
                        visible_step_ids.add(step_id)
            else:
                summary_count = 0
                for i in range(len(steps) - 1, -1, -1):
                    step = steps[i]
                    step_id = step["step_id"]
                    if step_id == full_info_step_id:
                        continue

                    meta = step.get("extra_metadata") or {}
                    step_subgoal_hash = meta.get("subgoal_hash")

                    # Keep steps under the active milestone, or slide back until min_summaries is met
                    if step_subgoal_hash == current_subgoal_hash or summary_count < min_summaries:
                        visible_step_ids.add(step_id)
                        summary_count += 1

        detailed_step_ids = set()
        # Pre-calculate steps that MUST be rendered in detailed mode (last N detailed)
        if last_n_detailed > 0:
            actual_n = min(len(steps), last_n_detailed)
            for step in steps[-actual_n:]:
                detailed_step_ids.add(step["step_id"])

            # Ensure the last step is always in detailed_step_ids
            detailed_step_ids.add(full_info_step_id)

        # M4: chunk blocks replace the per-step lines of already-chunked
        # ranges. Chunks only ever cover frozen (old) turns, so they are
        # emitted first, in chronological order; steps that must render
        # detailed (recent window / most-recent step) are never suppressed.
        chunked_ranges: list[tuple[int, int]] = []
        if chunks:
            for chunk in sorted(chunks, key=lambda c: c.get("start_step_number") or 0):
                text = chunk.get("text")
                if not text:
                    continue
                try:
                    start_n = int(chunk.get("start_step_number"))
                    end_n = int(chunk.get("end_step_number"))
                except (TypeError, ValueError):
                    continue
                chunked_ranges.append((start_n, end_n))
                output_parts.append(text)

        for step in steps:
            step_id = step["step_id"]

            # Skip steps that are compressed (not marked as visible)
            if step_id not in visible_step_ids:
                continue

            if chunked_ranges and step_id not in detailed_step_ids and step_id != full_info_step_id:
                step_number = step.get("step_number")
                if isinstance(step_number, int) and any(
                    start_n <= step_number <= end_n for start_n, end_n in chunked_ranges
                ):
                    continue  # represented by its chunk block above

            relative_time = step.get("relative_time") or "N/A"
            summary = step.get("summary")
            action = step.get("action_taken")
            thinking = step.get("operator_raw_thinking")
            result = step.get("last_execution_result")

            # Determine if this step should be detailed (e.g. current/recent step or missing summary)
            render_as_detailed = (
                all_detailed
                or (step_id in detailed_step_ids)
                or (not summary or summary == "Action executed.")
            )

            is_most_recent = step_id == full_info_step_id
            if render_as_detailed:
                step_line = _render_step_detailed(
                    step,
                    relative_time,
                    summary,
                    action,
                    result,
                    is_most_recent,
                )
            else:
                step_line = f"- *Step {step['step_number']} (Start: {relative_time}): {summary}*"
                if action:
                    action_clean = format_step_action_result(step)
                    step_line += f"\n  *Action*: {action_clean}"

            output_parts.append(step_line)

    return "\n".join(output_parts)


def get_active_subgoal_hashes(task_plan: str) -> tuple[str, str | None]:
    """Parses task_plan content to find the active (top-level, nested) subgoal hashes.

    Returns ``(parent_hash, sub_hash)``: the parent is the active top-level
    milestone (all active nested items consolidate under it); the sub hash is
    the bottom-most active nested item — the live sub-goal ledger leaf — or
    ``None`` when the milestone has no in-progress sub-goal. Falls back to the
    first pending subgoal if no active subgoal is found and all are pending.
    """
    if not task_plan:
        return "default", None

    try:
        snapshot = parse_plan(task_plan)

        # Bottom-most active item wins (most specific deeply nested subgoal)
        active_item = snapshot.last_active()
        if active_item is not None:
            if not active_item.is_top_level:
                parent = snapshot.parent_of(active_item)
                if parent is not None:
                    return parent.key, active_item.key
            return active_item.key, None

        # No active subgoal found. Check if all are completed.
        if all(item.is_done for item in snapshot.top_level):
            return "default", None

        # SAFE FALLBACK: if every item is untouched, the first pending one is active.
        if all(item.is_pending for item in snapshot.top_level):
            for item in snapshot.items:
                if item.is_pending:
                    return item.key, None

    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Unparseable plan text falls back to the default bucket.
        logger.debug("Active subgoal lookup fell back to default: %s", exc, exc_info=True)

    return "default", None


def get_completed_subgoal_hashes(task_plan: str) -> set[str]:
    """Extracts MD5 hashes of all completed [x] top-level subgoals from task_plan."""
    if not task_plan:
        return set()

    return {item.key for item in parse_plan(task_plan).top_level if item.is_done}


def get_all_subgoal_aliases(target_hash: str, base_dir: str | Path | None = None) -> set[str]:
    """Transitively resolves all older subgoal hashes that map to target_hash in subgoal_hash_chain.json."""
    aliases = {target_hash}
    if not base_dir or not isinstance(base_dir, (str, Path)):
        return aliases

    chain_path = Path(base_dir) / "notes" / "subgoal_hash_chain.json"
    if not chain_path.exists():
        return aliases

    try:
        chain = json.loads(chain_path.read_text(encoding="utf-8"))
    except Exception:
        return aliases

    # Transitively resolve old_hash -> new_hash chains
    for old_hash in chain:
        curr = old_hash
        visited = set()
        while curr in chain and curr not in visited:
            visited.add(curr)
            curr = chain[curr]
        if curr == target_hash:
            aliases.add(old_hash)

    return aliases


def get_recent_subgoal_hashes(
    steps: list, current_subgoal_hash: str, base_dir: str | Path | None = None
) -> set[str]:
    """Helper to get a set of subgoal hashes containing the current active subgoal hash,

    its historical failed/renamed aliases (resolved via
    subgoal_hash_chain.json),
    and the most recent completed subgoal hash as transition context.
    """
    # 1. Resolve all alias hashes for the active subgoal
    aliases = get_all_subgoal_aliases(current_subgoal_hash, base_dir)
    keep_hashes = set(aliases)

    # 2. Find the most recent completed subgoal in history
    # (meaning a step whose hash is NOT an alias of the active subgoal)
    recent_completed_hash = None
    for step in reversed(steps):
        meta = step.get("extra_metadata") or {}
        s_hash = meta.get("subgoal_hash")
        if s_hash and s_hash not in aliases:
            recent_completed_hash = s_hash
            break

    if recent_completed_hash:
        keep_hashes.add(recent_completed_hash)

    return keep_hashes
