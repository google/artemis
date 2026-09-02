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
from pathlib import Path
import re
from typing import Any

from artemis.utils.plan_grammar import parse_plan


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
        label = f"'{target_text}'" if target_text else "field"
        text_val = action_obj.get("text")
        clear_exist = action_obj.get("clear_exist") or action_obj.get("clear_before_input")
        clear_str = " (without clearing)" if clear_exist is False else ""
        return f"Inputted '{text_val}' into {label} at {coords}{clear_str}"
    elif act_type == "swipe":
        direction = action_obj.get("action_direction") or action_obj.get("direction")
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


def format_result_clean(result_obj) -> str | None:
    if not result_obj:
        return None
    if not isinstance(result_obj, dict):
        return str(result_obj)

    status = result_obj.get("status")
    repair_status = result_obj.get("repair_status")

    exec_list = result_obj.get("execution") or result_obj.get("executed_actions")
    if isinstance(exec_list, list) and exec_list:
        first_exec = exec_list[0]
        if isinstance(first_exec, dict):
            attempts = first_exec.get("attempts")
            error = first_exec.get("error") or result_obj.get("error")
            repair = first_exec.get("repair")

            if repair:
                if repair_status == "fixed":
                    return f"Repaired: {repair}"
                else:
                    return f"Error: {repair}"
            elif attempts and status == "failed":
                return f"Error: {' | '.join(attempts)}"
            elif error:
                return f"Error: {error}"
            elif status == "error":
                return "Error"
            else:
                return None

    error = result_obj.get("error")
    if error:
        return f"Error: {error}"
    elif status == "error":
        return "Error"

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
    except Exception:
        pass

    # Fallback to json.loads if literal_eval failed
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, (list, tuple)):
            return list(parsed)
    except Exception:
        pass

    return []


def format_tool_call_clean(name, args, result) -> str | None:
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
        "_exec_click",
        "_exec_click_sequence",
        "_exec_swipe",
        "_exec_input_text",
        "_exec_press_key",
        "_exec_long_press",
        "_exec_manage_app",
        "_exec_wait_for_delay",
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

    # Custom rendering for Operator call to failure_analyzer agent
    if name == "failure_analyzer":
        clean_args = {
            k: v
            for k, v in args.items()
            if k
            not in (
                "state",
                "tool_call_id",
                "pre_screenshot",
                "post_screenshot",
            )
        }
        args_str = json.dumps(clean_args, ensure_ascii=False)
        if len(args_str) > 120:
            args_str = args_str[:120] + "..."

        if isinstance(result, dict):
            status = result.get("status", "unknown")
            analysis = result.get("analysis") or result.get("reason") or "No analysis provided."
        else:
            status = args.get("status", "unknown")
            analysis = args.get("analysis", "No analysis provided.")
        if len(analysis) > 120:
            analysis = analysis[:120].strip() + "..."
        return f"`failure_analyzer({args_str})` -> Outcome: {status.upper()} (Analysis: {analysis})"

    # Custom rendering for Failure Analyzer final report
    if name == "report_failure_analysis":
        status = args.get("status", "unknown")
        analysis = args.get("analysis", "No analysis provided.")
        if len(analysis) > 120:
            analysis = analysis[:120].strip() + "..."
        return f"Outcome: {status.upper()} (Analysis: {analysis})"

    if name in ("read_note", "save_note", "update_note") and args.get("key") == "task_plan":
        return None

    # Custom rendering for all Action Tools (including _exec_ tools)
    if name in ACTION_TOOLS:
        action_name = name[6:] if name.startswith("_exec_") else name

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
            res_str = "\n".join(text_blocks)
        else:
            res_str = str(result)
    elif isinstance(result, dict):
        error = result.get("error")
        if error:
            res_str = f"Error: {error}"
        else:
            res_str = json.dumps(result, ensure_ascii=False)
            if len(res_str) > 120:
                res_str = res_str[:120] + "..."
    else:
        res_str = str(result)
        if len(res_str) > 120:
            res_str = res_str[:120] + "..."

    return f"`{name}({args_str})` -> {res_str}"


def format_step_action_result(step: dict) -> str:
    action = step.get("action_taken")
    action_parsed = action[0] if isinstance(action, list) and action else action
    action_clean = "No Action"
    if action_parsed:
        action_clean = format_action_clean(action_parsed)

    intercepted = False
    safety_net_detail = None
    interleaved = step.get("interleaved_events") or []
    for event in interleaved:
        if event.get("type") == "tool_call" and event.get("name") in (
            "safety_net_validation",
            "safety_net_pixel_validation",
        ):
            res_list = safe_parse_validation_result(event.get("result"))
            if len(res_list) > 0 and not res_list[0]:
                intercepted = True
                if len(res_list) >= 3:
                    safety_net_detail = res_list[2]
                break

    if not intercepted:
        result_obj = step.get("last_execution_result")
        if isinstance(result_obj, dict):
            exec_list = result_obj.get("execution") or []
            if exec_list:
                attempts = exec_list[0].get("attempts") or []
                if attempts and any(
                    "pre-execution validation" in str(att).lower()
                    or "validation failed" in str(att).lower()
                    for att in attempts
                ):
                    intercepted = True
                    safety_net_detail = attempts[0]

    recovery_actions = []
    for event in interleaved:
        e_type = event.get("type")
        name = event.get("name") or ""
        if (
            e_type == "tool_call"
            and name.startswith("_exec_")
            and name != "report_failure_analysis"
        ):
            formatted = format_tool_call_clean(name, event.get("args") or {}, event.get("result"))
            if formatted:
                recovery_actions.append(formatted)

    if not recovery_actions:
        legacy_tool_calls = step.get("tool_calls") or []
        for tc in legacy_tool_calls:
            name = tc.get("name") or ""
            if name.startswith("_exec_") and name != "report_failure_analysis":
                payload = tc.get("payload") or {}
                tc_args = payload.get("args") or tc.get("args") or {}
                tc_result = (
                    payload.get("result") or payload.get("error") or tc.get("result") or "No result"
                )
                formatted = format_tool_call_clean(name, tc_args, tc_result)
                if formatted:
                    recovery_actions.append(formatted)

    output_parts = [action_clean]

    if intercepted:
        detail_msg = f": {str(safety_net_detail).strip()}" if safety_net_detail else ""
        output_parts.append(f"(Action not executed{detail_msg})")

    if not intercepted and not recovery_actions:
        result_obj = step.get("last_execution_result")
        if isinstance(result_obj, dict) and result_obj.get("status") == "failed":
            error_msg = None
            exec_list = result_obj.get("execution") or []
            if exec_list and isinstance(exec_list[0], dict):
                error_msg = exec_list[0].get("error")
            if not error_msg:
                error_msg = result_obj.get("error")
            if error_msg:
                output_parts.append(f"(Execution failed: {str(error_msg).strip()})")

    if recovery_actions:
        recovery_str = ", ".join(recovery_actions)
        output_parts.append(f"Recovery Actions: [{recovery_str}]")

    return " -> ".join(output_parts)


def _render_step_detailed(
    step: dict,
    relative_time: str,
    summary: str,
    action: Any,
    result: Any,
    is_most_recent: bool,
    for_failure_analyzer: bool = False,
) -> str:
    # 1. Step Header
    if is_most_recent and for_failure_analyzer:
        status_str = (
            "Most Recent Step (Failed to execute, this is the step you need to"
            " focus on and repair), "
        )
    else:
        status_str = "Most Recent Step, " if is_most_recent else ""

    # Summary is omitted from the detailed view header as it is already fully detailed below.
    step_line = f"- **Step {step['step_number']} ({status_str}Start: {relative_time})**"

    interleaved = step.get("interleaved_events") or []

    # 2. Determine if Safety Net validation failed (intercepted)
    intercepted = False
    safety_net_detail = None
    for event in interleaved:
        if event.get("type") == "tool_call" and event.get("name") in (
            "safety_net_validation",
            "safety_net_pixel_validation",
        ):
            res_list = safe_parse_validation_result(event.get("result"))
            if len(res_list) > 0 and not res_list[0]:
                intercepted = True
                if len(res_list) >= 3:
                    safety_net_detail = res_list[2]
                break

    # Fallback check on result structure for legacy/non-interleaved steps
    if not intercepted and result:
        exec_list = result.get("execution") or []
        if exec_list:
            attempts = exec_list[0].get("attempts") or []
            if attempts and any(
                "pre-execution validation" in str(att).lower()
                or "validation failed" in str(att).lower()
                for att in attempts
            ):
                intercepted = True
                safety_net_detail = attempts[0]

    # 3. Categorize events into Operator and Failure Analyzer
    operator_events = []
    failure_analyzer_events = []

    # Internal tools list to filter out from Operator
    INTERNAL_SYSTEM_TOOLS = {
        "safety_net_validation",
        "safety_net_pixel_validation",
        "failure_analyzer",
        "report_failure_analysis",
        "hopper",
    }

    if interleaved:
        for event in interleaved:
            e_type = event.get("type")
            name = event.get("name") or ""
            content = event.get("content") or ""

            # Skip the failure_analyzer agent call itself to prevent duplicates
            if name == "failure_analyzer":
                continue

            if e_type in ("thought", "native_thought"):
                operator_events.append(event)
            elif (
                e_type == "tool_call"
                and name not in INTERNAL_SYSTEM_TOOLS
                and not name.startswith("_exec_")
            ):
                operator_events.append(event)
            elif e_type in (
                "failure_analyzer_thought",
                "failure_analyzer_native_thought",
            ):
                failure_analyzer_events.append(event)
            elif e_type == "tool_call" and (
                name.startswith("_exec_") or name == "report_failure_analysis"
            ):
                failure_analyzer_events.append(event)
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
            name = tc.get("name")
            if name in INTERNAL_SYSTEM_TOOLS or name.startswith("_exec_"):
                # If it's failure analyzer tools in legacy steps, place them in failure analyzer
                if name.startswith("_exec_") or name == "report_failure_analysis":
                    # Convert to interleaved style format
                    payload = tc.get("payload") or {}
                    tc_args = payload.get("args") or tc.get("args") or {}
                    tc_result = (
                        payload.get("result")
                        or payload.get("error")
                        or tc.get("result")
                        or "No result"
                    )
                    failure_analyzer_events.append(
                        {
                            "type": "tool_call",
                            "name": name,
                            "args": tc_args,
                            "result": tc_result,
                        }
                    )
            else:
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
    if operator_events and (not for_failure_analyzer or is_most_recent):
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
                    name, event.get("args") or {}, event.get("result")
                )
                if formatted:
                    step_line += f"\n    - [Tool Call]: {formatted}"

    # Check if the execution failed
    exec_error = None
    if result and isinstance(result, dict):
        status = result.get("status")
        if status == "failed" or status == "error":
            exec_list = result.get("execution") or result.get("executed_actions")
            if isinstance(exec_list, list) and exec_list and isinstance(exec_list[0], dict):
                first_exec = exec_list[0]
                attempts = first_exec.get("attempts")
                if attempts:
                    exec_error = " | ".join(attempts)
                else:
                    exec_error = first_exec.get("error") or result.get("error")
            else:
                exec_error = result.get("error")

    # 5. Render Planned Action
    if action:
        action_clean = format_action_clean(action)
        if intercepted:
            step_line += (
                f"\n  * [Planned Action]: {action_clean} (Intercepted by Pre-Execution Safety Net)"
            )
        else:
            if exec_error:
                step_line += (
                    f"\n  * [Planned Action]: {action_clean} -> (Execution failed: {exec_error})"
                )
            else:
                step_line += f"\n  * [Planned Action]: {action_clean}"

    # 6. Render Pre-Execution Safety Net
    if intercepted:
        step_line += "\n  * [Pre-Execution Safety Net]:"
        detail_str = (
            f"(Action not executed: {str(safety_net_detail).strip()})"
            if safety_net_detail
            else "(Action not executed)"
        )
        step_line += f"\n    - [Safety Net Check]: {detail_str}"

    # 7. Render Failure Analyzer Recovery Loop
    if failure_analyzer_events:
        step_line += "\n  * [Failure Analyzer Recovery Loop]:"
        for event in failure_analyzer_events:
            e_type = event["type"]
            content = event.get("content") or ""
            name = event.get("name") or ""
            if not content.strip() and e_type != "tool_call":
                continue
            if e_type in (
                "failure_analyzer_native_thought",
                "failure_analyzer_thought",
                "failure_analyzer_monologue",
            ):
                cleaned = content.strip()
                if cleaned:
                    step_line += f"\n    - {cleaned}"
            elif e_type == "tool_call":
                if name == "report_failure_analysis":
                    continue
                formatted = format_tool_call_clean(
                    name, event.get("args") or {}, event.get("result")
                )
                if formatted:
                    step_line += f"\n    - [Tool Call]: {formatted}"

    # Check if Failure Analyzer was active and reported
    has_fa = bool(failure_analyzer_events)

    # We consider FA as reported if it called the report tool,
    # OR if the step's last_execution_result already contains a legacy repair description!
    has_repair_in_result = False
    if result and isinstance(result, dict):
        exec_list = result.get("execution") or result.get("executed_actions")
        if isinstance(exec_list, list) and exec_list and isinstance(exec_list[0], dict):
            if exec_list[0].get("repair"):
                has_repair_in_result = True

    fa_reported = (
        any(
            event.get("type") == "tool_call" and event.get("name") == "report_failure_analysis"
            for event in failure_analyzer_events
        )
        or has_repair_in_result
    )

    # 8. Validator Execution Result
    if has_fa and not fa_reported:
        step_line += (
            "\n  * [Result]: Unknown repair outcome (Failure Analyzer did not"
            " submit a final report)"
        )
    elif result:
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
    for_failure_analyzer: bool = False,
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

    # Check if task_plan has actual subgoals (e.g. starts with '- [')
    has_subgoals = task_plan and any(
        line.strip().startswith("- [") for line in task_plan.split("\n")
    )

    if has_subgoals:
        output_parts.append("--- Task Plan ---")
        output_parts.append(task_plan)
        if for_failure_analyzer:
            output_parts.append(
                "*(Note: Provided for context only. Do not execute the remaining plan.)*"
            )
        output_parts.append("")
    elif not for_failure_analyzer:
        # For non-failure_analyzer context, print fallback if not empty
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
            for chunk in sorted(
                chunks, key=lambda c: c.get("start_step_number") or 0
            ):
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
                    for_failure_analyzer,
                )
            else:
                step_line = f"- *Step {step['step_number']} (Start: {relative_time}): {summary}*"
                if action:
                    action_clean = format_step_action_result(step)
                    step_line += f"\n  *Action*: {action_clean}"

            output_parts.append(step_line)

    return "\n".join(output_parts)


def get_active_subgoal_hashes(task_plan: str) -> tuple[str, str | None]:
    """Parses task_plan content to find the active top-level subgoal hash.

    Consolidates all active subtasks under their parent Level 1 subgoal,
    returning (parent_hash, None). Falls back to the first pending subgoal if no
    active subgoal is found and all are pending.
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
                    return parent.key, None
            return active_item.key, None

        # No active subgoal found. Check if all are completed.
        if all(item.is_done for item in snapshot.top_level):
            return "default", None

        # SAFE FALLBACK: if every item is untouched, the first pending one is active.
        if all(item.is_pending for item in snapshot.top_level):
            for item in snapshot.items:
                if item.is_pending:
                    return item.key, None

    except Exception:
        pass

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
