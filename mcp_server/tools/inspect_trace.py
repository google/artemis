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

"""MCP Tool: mobile_inspect_trace."""

import json
import math
import os
import sqlite3
import sys
from typing import Any
from PIL import Image, ImageDraw

from mcp_server.base import mcp
from mcp_server.utils import env_utils
from artemis.runtime import trace_store
from artemis.utils.visualization import draw_action_overlay_on_image
from artemis.utils.task_tree import (
    _render_step_detailed,
    build_plan_and_history,
)


def _draw_action_overlay(bg_image_path: str, action_obj: dict, output_path: str) -> bool:
    """Draws the action overlay on the image using Artemis's native visualization engine."""
    try:
        if not os.path.exists(bg_image_path):
            return False

        with open(bg_image_path, "rb") as f:
            raw_bytes = f.read()

        act_name = action_obj.get("action") or action_obj.get("name") or ""
        action_args = dict(action_obj.get("args") or {})
        for k in (
            "target",
            "coordinates",
            "point",
            "direction",
            "sequence",
            "targets",
            "normalized_coordinates",
        ):
            if k in action_obj and k not in action_args:
                action_args[k] = action_obj[k]

        annotated_bytes = draw_action_overlay_on_image(
            image_bytes=raw_bytes,
            action_name=act_name,
            action_args=action_args,
        )

        if not annotated_bytes or annotated_bytes == raw_bytes:
            return False

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(annotated_bytes)
        return True
    except Exception as e:
        print(f"Error drawing action overlay: {e}", file=sys.stderr)
        return False


@mcp.tool()
async def mobile_inspect_trace(action: str, trace_id: str, step_number: int | None = None) -> Any:
    """Retrieves execution details of a mobile automation task (running or finished).

    Use it to monitor progress, verify answers, and diagnose agent errors for
    both Flash and Pro tasks. Every result includes the assigned `device_serial`.

    ### Actions
    - **'view_summary'**: High-level execution summary across all steps.
      Pro: hierarchical task plan with per-step status; Flash: full execution
      chain with each step's reasoning and action.
    - **'view_step_screenshots'**: Local file paths of one step's screenshots:
      `before_screenshot` (what the agent saw), `after_screenshot` (only set
      when the action failed/was intercepted, else null), and
      `action_overlay_screenshot` (the action visually marked — e.g. red circle
      for taps — key for verifying the agent tapped the right element).
    - **'view_step_details'**: Deep per-step diagnostics (raw VLM thoughts,
      exact actions). Pro only — DO NOT use for Flash tasks ('view_summary'
      already carries their full execution chain).

    Args:
        action: `"view_summary"`, `"view_step_screenshots"`, or
          `"view_step_details"`.
        trace_id: The task's session identifier from `mobile_run_task`.
        step_number: 1-indexed step to query; required for the two per-step
          actions, omit for `view_summary`.
    """
    project_root = env_utils.get_project_root()
    db_path = os.path.join(trace_store.TRACES_DIR, "data_engine.db")
    if not os.path.exists(db_path):
        db_path = os.path.join(project_root, "traces", "data_engine.db")

    if not os.path.exists(db_path):
        return {
            "error": "Database not found",
            "message": f"Data engine database does not exist at {db_path} yet.",
        }

    # 1. Handle Action: "view_summary"
    if action == "view_summary":
        plan_content = None
        plan_path = os.path.join(trace_store.get_trace_dir(trace_id), "notes", "task_plan.md")
        if not os.path.exists(plan_path):
            plan_path = os.path.join(trace_store.TRACES_DIR, "notes", "task_plan.md")
        if os.path.exists(plan_path):
            try:
                with open(plan_path, encoding="utf-8") as f:
                    plan_content = f.read()
            except OSError:
                # Unreadable plan file: the summary just omits the plan.
                pass

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(
                "SELECT start_time FROM sessions WHERE session_id = ?",
                (trace_id,),
            )
            session_row = cursor.fetchone()
            session_start_time = session_row["start_time"] if session_row else None

            cursor.execute(
                "SELECT * FROM steps WHERE session_id = ? ORDER BY step_number ASC",
                (trace_id,),
            )
            rows = cursor.fetchall()

            steps = []
            for r in rows:
                step_dict = dict(r)
                for col in ("action_taken", "last_execution_result", "extra_metadata"):
                    if step_dict.get(col):
                        try:
                            step_dict[col] = json.loads(step_dict[col])
                        except (ValueError, TypeError):
                            # Non-JSON column value: keep the raw string.
                            pass

                cursor.execute(
                    "SELECT type, name, status, timestamp, duration, payload FROM traces WHERE step_id = ? ORDER BY timestamp ASC",
                    (step_dict["step_id"],),
                )
                trace_rows = cursor.fetchall()
                events = []
                for tr in trace_rows:
                    t_dict = dict(tr)
                    if t_dict.get("payload"):
                        try:
                            t_dict["payload"] = json.loads(t_dict["payload"])
                        except (ValueError, TypeError):
                            # Non-JSON payload: keep the raw string.
                            pass
                    events.append(t_dict)
                step_dict["interleaved_events"] = events

                if session_start_time and step_dict.get("timestamp"):
                    relative_time_val = step_dict["timestamp"] - session_start_time
                    step_dict["relative_time"] = f"{relative_time_val:.1f}s"
                else:
                    step_dict["relative_time"] = "N/A"

                steps.append(step_dict)
            conn.close()
        except Exception as e:
            return {
                "error": "Database query error",
                "message": f"Failed to retrieve steps from database: {e}",
            }

        status_data = trace_store.read_status(trace_id)
        is_flash = status_data and status_data.get("model", "").lower() == "flash"
        device_serial = status_data.get("device_serial") if status_data else None
        if not device_serial and os.path.exists(db_path):
            try:
                conn_dev = sqlite3.connect(db_path)
                conn_dev.row_factory = sqlite3.Row
                cur_dev = conn_dev.cursor()
                cur_dev.execute(
                    "SELECT device_info FROM sessions WHERE session_id = ? LIMIT 1",
                    (trace_id,),
                )
                row_dev = cur_dev.fetchone()
                if row_dev and row_dev["device_info"]:
                    try:
                        d_info = json.loads(row_dev["device_info"])
                        if isinstance(d_info, dict) and d_info.get("device_id"):
                            device_serial = d_info["device_id"]
                    except (ValueError, TypeError):
                        # Malformed device_info JSON: leave the serial unset.
                        pass
                conn_dev.close()
            except sqlite3.Error:
                # The serial enriches the summary header only; skip it.
                pass

        device_info_str = f" | **Device Serial:** `{device_serial}`" if device_serial else ""

        if is_flash:
            summary_lines = [
                "# ARTEMIS Flash Execution Summary",
                f"**Session ID:** `{trace_id}` | **Model:** Flash{device_info_str}\n",
                "---",
                "## Step-by-Step Execution Chain\n",
            ]
            if not steps:
                summary_lines.append("_No action steps have been recorded yet._")
            else:
                for s in steps:
                    s_num = s.get("step_number", "?")
                    rel_t = s.get("relative_time", "N/A")

                    dur_val = s.get("duration")
                    if dur_val is None and s.get("interleaved_events"):
                        events = s["interleaved_events"]
                        timestamps = [e.get("timestamp", 0) for e in events if e.get("timestamp")]
                        if timestamps:
                            dur_val = max(timestamps) - min(timestamps)
                    dur_str = f"{dur_val:.1f}s" if dur_val is not None and dur_val > 0 else "< 0.5s"

                    thought = s.get("operator_raw_thinking")
                    if not thought or not thought.strip():
                        for ev in s.get("interleaved_events", []):
                            if ev.get("type") in ("raw_thinking", "llm_call") and ev.get("payload"):
                                p = ev["payload"]
                                if isinstance(p, dict) and p.get("response_text"):
                                    thought = p["response_text"]
                                    break
                    if not thought or not thought.strip():
                        thought = "(Direct action without preliminary text reasoning)"

                    act = s.get("action_taken") or "N/A"
                    if isinstance(act, dict) and "action" in act:
                        act_str = f"{act['action']}({act.get('args', act.get('coordinates', ''))})"
                    else:
                        act_str = str(act)
                    res = s.get("last_execution_result") or "N/A"
                    summary_lines.append(f"### Step {s_num} (at {rel_t}, duration {dur_str})")
                    summary_lines.append(f"- **Thinking / Motivation:** {thought}")
                    summary_lines.append(f"- **Action Taken:** `{act_str}` -> `{res}`\n")
            return "\n".join(summary_lines)

        try:
            summary_markdown = build_plan_and_history(
                task_plan=plan_content or "No task plan created yet.",
                steps=steps,
                current_subgoal_hash="default",
                min_summaries=len(steps),
                last_n_detailed=0,
                all_detailed=False,
            )
            if device_serial and not summary_markdown.startswith("**Session ID:"):
                summary_markdown = f"**Session ID:** `{trace_id}` | **Model:** Pro{device_info_str}\n\n" + summary_markdown
            return summary_markdown
        except Exception as e:
            return {
                "error": "Formatting error",
                "message": f"Failed to format plan and history using Artemis task tree: {e}",
            }

    # 2. Handle Actions requiring step_number
    elif action in ("view_step_screenshots", "view_step_details"):
        status_data = trace_store.read_status(trace_id)
        is_flash = status_data and status_data.get("model", "").lower() == "flash"
        device_serial = status_data.get("device_serial") if status_data else None

        if action == "view_step_details" and is_flash:
            return {
                "error": "Not applicable for Flash model",
                "message": (
                    "Action 'view_step_details' is not needed for Flash model runs. "
                    "For Flash tasks, 'view_summary' already outputs the complete "
                    "step-by-step reasoning (thoughts) and executed actions for the entire run."
                ),
            }

        if step_number is None:
            return {
                "error": "Missing parameter",
                "message": f"Parameter 'step_number' is required for action '{action}'.",
            }

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM steps WHERE session_id = ? AND step_number = ?",
                (trace_id, step_number),
            )
            row = cursor.fetchone()

            if not row:
                conn.close()
                return {
                    "error": "Step not found",
                    "message": f"Step number {step_number} not found for trace '{trace_id}'.",
                }

            step_dict = dict(row)
            for col in ("action_taken", "last_execution_result", "extra_metadata"):
                if step_dict.get(col):
                    try:
                        step_dict[col] = json.loads(step_dict[col])
                    except (ValueError, TypeError):
                        # Non-JSON column value: keep the raw string.
                        pass

            cursor.execute(
                "SELECT start_time FROM sessions WHERE session_id = ?",
                (trace_id,),
            )
            session_row = cursor.fetchone()
            session_start_time = session_row["start_time"] if session_row else None

            cursor.execute(
                "SELECT type, name, status, timestamp, duration, payload FROM traces WHERE step_id = ? ORDER BY timestamp ASC",
                (step_dict["step_id"],),
            )
            trace_rows = cursor.fetchall()
            events = []
            for tr in trace_rows:
                t_dict = dict(tr)
                if t_dict.get("payload"):
                    try:
                        t_dict["payload"] = json.loads(t_dict["payload"])
                    except (ValueError, TypeError):
                        # Non-JSON payload: keep the raw string.
                        pass
                events.append(t_dict)
            step_dict["interleaved_events"] = events
            conn.close()
        except Exception as e:
            return {
                "error": "Database error",
                "message": f"Database query failed: {e}",
            }

        # 3. Handle Action: "view_step_screenshots"
        if action == "view_step_screenshots":
            images_dir = os.path.join(trace_store.TRACES_DIR, "images")
            if not os.path.exists(images_dir):
                images_dir = os.path.join(project_root, "traces", "images")

            pre_image = None
            post_image = None
            overlay_image = None

            if step_dict.get("pre_image_name"):
                name = step_dict["pre_image_name"]
                if not name.endswith(".jpg"):
                    name += ".jpg"
                path = os.path.join(images_dir, name)
                if os.path.exists(path):
                    pre_image = f"file://{path}"

                    overlay_dir = trace_store.get_trace_dir(trace_id)
                    os.makedirs(overlay_dir, exist_ok=True)
                    overlay_filename = f"step_{step_dict['step_number']}_overlay.jpg"
                    overlay_abs_path = os.path.join(overlay_dir, overlay_filename)

                    action_obj = step_dict.get("action_taken")
                    action_parsed = (
                        action_obj[0] if isinstance(action_obj, list) and action_obj else action_obj
                    )

                    if isinstance(action_parsed, dict) and (action_parsed.get("action") or action_parsed.get("name")):
                        if not os.path.exists(overlay_abs_path):
                            success_draw = _draw_action_overlay(
                                path, action_parsed, overlay_abs_path
                            )
                            if success_draw:
                                overlay_image = f"file://{overlay_abs_path}"
                        else:
                            overlay_image = f"file://{overlay_abs_path}"

            if step_dict.get("post_image_name"):
                name = step_dict["post_image_name"]
                if not name.endswith(".jpg"):
                    name += ".jpg"
                path = os.path.join(images_dir, name)
                if os.path.exists(path):
                    post_image = f"file://{path}"

            return {
                "trace_id": trace_id,
                "device_serial": device_serial,
                "step_number": step_dict["step_number"],
                "before_screenshot": pre_image,
                "after_screenshot": post_image,
                "action_overlay_screenshot": overlay_image,
            }

        # 4. Handle Action: "view_step_details"
        elif action == "view_step_details":
            relative_time_str = "N/A"
            if session_start_time and step_dict.get("timestamp"):
                relative_time_val = step_dict["timestamp"] - session_start_time
                relative_time_str = f"{relative_time_val:.1f}s"

            rendered_text = _render_step_detailed(
                step=step_dict,
                relative_time=relative_time_str,
                summary=step_dict.get("summary"),
                action=step_dict.get("action_taken"),
                result=step_dict.get("last_execution_result"),
                is_most_recent=False,
                for_failure_analyzer=False,
            )

            return {
                "trace_id": trace_id,
                "device_serial": device_serial,
                "step_number": step_dict["step_number"],
                "details": rendered_text,
            }

    else:
        return {
            "error": "Invalid action",
            "message": (
                f"Action '{action}' is not supported. Supported actions: "
                "'view_summary', 'view_step_screenshots', 'view_step_details'."
            ),
        }
