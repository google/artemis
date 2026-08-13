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

import json
from typing import Any

try:
    from admin_console.database.connection import db_session
except ImportError:
    from apps.admin_console.database.connection import db_session


class StepRepository:
    """Repository handling querying and formatting of steps and associated traces."""

    def __init__(self, db_path=None):
        self.db_path = db_path

    def get_step_session_id(self, step_id: str) -> str | None:
        with db_session(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT session_id FROM steps WHERE step_id = ?", (step_id,))
            row = cursor.fetchone()
            return row["session_id"] if row else None

    @staticmethod
    def _clean_value(val: Any) -> Any:
        """Recursively cleans values to remove non-serializable and internal
        Python runtime representations.
        """
        if val is None:
            return None
        if isinstance(val, (bool, int, float)):
            return val
        if isinstance(val, str):
            if (
                "object at 0x" in val
                or val.startswith("<artemis.")
                or val.startswith("<controller")
            ):
                return None
            return val
        if isinstance(val, dict):
            cleaned = {}
            for k, v in val.items():
                cv = StepRepository._clean_value(v)
                if cv is not None:
                    cleaned[k] = cv
            return cleaned
        if isinstance(val, (list, tuple)):
            cleaned_list = []
            for item in val:
                cv = StepRepository._clean_value(item)
                if cv is not None:
                    cleaned_list.append(cv)
            return cleaned_list
        val_str = str(val)
        if "object at 0x" in val_str or val_str.startswith("<"):
            return None
        return val_str

    @staticmethod
    def _extract_clean_text_and_images(content: Any) -> tuple[str, int]:
        """Extracts readable text and count of image blocks from LLM content."""
        if not content:
            return "", 0
        if isinstance(content, str):
            if content.startswith("data:image/") or "base64," in content[:50]:
                return "", 1
            return content, 0
        if isinstance(content, dict):
            if content.get("type") == "image_url" or "image_url" in content:
                return "", 1
            if content.get("type") == "text":
                return str(content.get("text", "")), 0
            t = content.get("text") or content.get("thought") or ""
            return str(t), 0
        if isinstance(content, list):
            total_text = ""
            num_images = 0
            for item in content:
                txt, imgs = StepRepository._extract_clean_text_and_images(item)
                total_text += txt
                num_images += imgs
            return total_text, num_images
        return str(content), 0

    @staticmethod
    def _extract_token_usage_from_trace(payload_raw: Any) -> tuple[int, int, int]:
        """Extract prompt, completion, and total tokens from an LLM trace payload with multimodal awareness."""
        if not payload_raw:
            return 0, 0, 0
        payload_obj = payload_raw
        if isinstance(payload_raw, str):
            try:
                payload_obj = json.loads(payload_raw)
            except Exception:
                pass
        if not isinstance(payload_obj, dict):
            return 0, 0, 0

        u = payload_obj.get("token_usage") or payload_obj.get("usage_metadata")
        if isinstance(u, dict):
            pr = u.get("prompt_tokens") or u.get("prompt_token_count") or u.get("input_tokens") or 0
            co = (
                u.get("completion_tokens")
                or u.get("candidates_token_count")
                or u.get("output_tokens")
                or 0
            )
            to = u.get("total_tokens") or u.get("total_token_count") or (pr + co)
            if to > 0:
                return int(pr), int(co), int(to)

        # Fallback for historical traces: estimate tokens from text and images
        msgs = payload_obj.get("messages", [])
        resp = payload_obj.get("response", [])

        prompt_text = ""
        prompt_images = 0
        if isinstance(msgs, list):
            for m in msgs:
                if isinstance(m, dict):
                    t, imgs = StepRepository._extract_clean_text_and_images(m.get("content"))
                    prompt_text += t
                    prompt_images += imgs
                else:
                    t, imgs = StepRepository._extract_clean_text_and_images(m)
                    prompt_text += t
                    prompt_images += imgs
        else:
            t, imgs = StepRepository._extract_clean_text_and_images(msgs)
            prompt_text += t
            prompt_images += imgs

        resp_text = ""
        resp_images = 0
        if isinstance(resp, list):
            for r in resp:
                if isinstance(r, dict):
                    t, imgs = StepRepository._extract_clean_text_and_images(r.get("content"))
                    resp_text += t
                    resp_images += imgs
                else:
                    t, imgs = StepRepository._extract_clean_text_and_images(r)
                    resp_text += t
                    resp_images += imgs

        # Standard Gemini multimodal token pricing: 258 tokens per image
        pr = (len(prompt_text) // 4) + (prompt_images * 258)
        co = (len(resp_text) // 4) + (resp_images * 258)
        return int(pr), int(co), int(pr + co)

    def _clean_tool_payload(self, payload_raw: Any, trace_type: str | None = None) -> Any:
        if not payload_raw:
            return None
        payload_obj = payload_raw
        if isinstance(payload_raw, str):
            try:
                payload_obj = json.loads(payload_raw)
            except Exception:
                pass

        if not isinstance(payload_obj, dict):
            return None

        if trace_type == "llm_call":
            return (
                {"error": self._clean_value(payload_obj.get("error"))}
                if "error" in payload_obj
                else None
            )

        args = payload_obj.get("args", payload_obj)
        if isinstance(args, dict):
            return {"args": self._clean_value(args)}
        return {"args": self._clean_value(args)}

    def get_session_steps(self, session_id: str, client: str | None = None) -> list[dict[str, Any]]:
        with db_session(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM steps WHERE session_id = ? ORDER BY step_number ASC",
                (session_id,),
            )
            rows = cursor.fetchall()

            steps = []
            for r in rows:
                step_dict = dict(r)

                # Strip raw binary bytes for API efficiency (clients fetch images by url/hash)
                step_dict["pre_screenshot_bytes"] = None
                step_dict["post_screenshot_bytes"] = None

                # Parse JSON fields safely
                for json_col in [
                    "action_taken",
                    "last_execution_result",
                    "extra_metadata",
                ]:
                    if step_dict.get(json_col):
                        try:
                            step_dict[json_col] = json.loads(step_dict[json_col])
                        except Exception:
                            pass

                # If post_image_name is identical to pre_image_name, normalize to None
                pre_img = step_dict.get("pre_image_name")
                if pre_img and step_dict.get("post_image_name") == pre_img:
                    step_dict["post_image_name"] = None

                # Fetch generic tool traces for this step
                step_id = step_dict.get("step_id")
                if step_id:
                    cursor.execute(
                        "SELECT t1.trace_id, t1.parent_trace_id, t1.step_id,"
                        " t1.type, t1.name, t1.timestamp, t1.duration, t1.status,"
                        " t1.payload, t2.name as agent_name FROM traces t1 LEFT"
                        " JOIN traces t2 ON t1.parent_trace_id = t2.trace_id WHERE"
                        " t1.step_id = ? AND (t1.type = 'tool' OR (t1.type ="
                        " 'llm_call' AND t1.status = 'failed')) ORDER BY"
                        " t1.timestamp ASC",
                        (step_id,),
                    )
                    trace_rows = cursor.fetchall()
                    generic_tools = []
                    for tr in trace_rows:
                        trace_dict = dict(tr)
                        trace_dict["payload"] = self._clean_tool_payload(
                            trace_dict.get("payload"), trace_dict.get("type")
                        )
                        generic_tools.append(trace_dict)
                    step_dict["generic_tools"] = generic_tools

                    # Compute token usage for this step from all LLM call traces
                    cursor.execute(
                        "SELECT payload FROM traces WHERE step_id = ? AND type = 'llm_call'",
                        (step_id,),
                    )
                    llm_rows = cursor.fetchall()
                    step_p, step_c, step_t = 0, 0, 0
                    for lr in llm_rows:
                        p, c, t = self._extract_token_usage_from_trace(lr["payload"])
                        step_p += p
                        step_c += c
                        step_t += t

                    # If step_t is 0, also check extra_metadata for token_usage
                    if step_t == 0 and step_dict.get("extra_metadata"):
                        meta = (
                            step_dict["extra_metadata"]
                            if isinstance(step_dict["extra_metadata"], dict)
                            else {}
                        )
                        u = meta.get("token_usage")
                        if isinstance(u, dict):
                            step_p = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
                            step_c = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
                            step_t = int(u.get("total_tokens") or (step_p + step_c))

                    step_dict["token_usage"] = {
                        "prompt_tokens": step_p,
                        "completion_tokens": step_c,
                        "total_tokens": step_t,
                    }
                    step_dict["total_tokens"] = step_t
                else:
                    step_dict["generic_tools"] = []
                    step_dict["token_usage"] = {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    }
                    step_dict["total_tokens"] = 0

                try:
                    from artemis.utils.coordinates import normalize_step_actions

                    step_dict = normalize_step_actions(step_dict)
                except ImportError:
                    pass

                steps.append(step_dict)

            # Fetch step-less tool traces (e.g. planner tools)
            cursor.execute(
                "SELECT t1.trace_id, t1.parent_trace_id, t1.step_id, t1.type,"
                " t1.name, t1.timestamp, t1.duration, t1.status, t1.payload,"
                " t2.name as agent_name FROM traces t1 LEFT JOIN traces t2 ON"
                " t1.parent_trace_id = t2.trace_id WHERE t1.session_id = ? AND"
                " (t1.step_id IS NULL OR t1.step_id = '') AND (t1.type = 'tool' OR"
                " (t1.type = 'llm_call' AND t1.status = 'failed')) ORDER BY"
                " t1.timestamp ASC",
                (session_id,),
            )
            stepless_rows = cursor.fetchall()
            if stepless_rows:
                unassigned_tools = []
                sorted_steps = sorted(steps, key=lambda s: s.get("timestamp") or 0)

                for tr in stepless_rows:
                    trace_dict = dict(tr)
                    trace_dict["payload"] = self._clean_tool_payload(
                        trace_dict.get("payload"), trace_dict.get("type")
                    )

                    tr_time = trace_dict.get("timestamp") or 0
                    target_step = None
                    for s in sorted_steps:
                        s_time = s.get("timestamp") or 0
                        if s_time <= tr_time:
                            target_step = s
                        else:
                            break

                    if target_step:
                        if "generic_tools" not in target_step or not target_step["generic_tools"]:
                            target_step["generic_tools"] = []
                        target_step["generic_tools"].append(trace_dict)
                        target_step["generic_tools"].sort(key=lambda t: t.get("timestamp") or 0)
                    else:
                        unassigned_tools.append(trace_dict)

                if unassigned_tools:
                    unassigned_tools.sort(key=lambda t: t.get("timestamp") or 0)
                    first_timestamp = unassigned_tools[0].get("timestamp") or 0

                    # Calculate token usage ONLY for pre-planning LLM calls (before first step)
                    first_recorded_step_time = (
                        sorted_steps[0].get("timestamp") if sorted_steps else float("inf")
                    )
                    cursor.execute(
                        "SELECT payload FROM traces WHERE session_id = ? AND (step_id IS NULL OR step_id = '') AND timestamp < ? AND type = 'llm_call'",
                        (session_id, first_recorded_step_time),
                    )
                    plan_llm_rows = cursor.fetchall()
                    plan_p, plan_c, plan_t = 0, 0, 0
                    for lr in plan_llm_rows:
                        p, c, t = self._extract_token_usage_from_trace(lr["payload"])
                        plan_p += p
                        plan_c += c
                        plan_t += t

                    # Retrieve planner thinking / thoughts
                    cursor.execute(
                        "SELECT type, payload FROM traces WHERE session_id = ? AND (step_id IS NULL OR step_id = '') AND timestamp < ? AND type IN ('raw_thinking', 'thinking') ORDER BY timestamp ASC",
                        (session_id, first_recorded_step_time),
                    )
                    think_rows = cursor.fetchall()
                    p_raw_th = []
                    p_nat_th = []
                    for thr in think_rows:
                        if thr["payload"]:
                            try:
                                th_obj = (
                                    json.loads(thr["payload"])
                                    if isinstance(thr["payload"], str)
                                    else thr["payload"]
                                )
                                th_text = th_obj.get("thought") or th_obj.get("text") or ""
                                if thr["type"] == "raw_thinking" and th_text:
                                    p_raw_th.append(str(th_text).strip())
                                elif thr["type"] == "thinking" and th_text:
                                    p_nat_th.append(str(th_text).strip())
                            except Exception:
                                pass

                    virtual_step = {
                        "step_id": "pre-planning",
                        "step_type": "planning",
                        "session_id": session_id,
                        "step_number": 0,
                        "timestamp": first_timestamp,
                        "operator_native_thinking": "\n\n".join(p_nat_th) if p_nat_th else "",
                        "operator_raw_thinking": "\n\n".join(p_raw_th) if p_raw_th else "",
                        "action_taken": None,
                        "last_execution_result": None,
                        "extra_metadata": None,
                        "generic_tools": unassigned_tools,
                        "token_usage": {
                            "prompt_tokens": plan_p,
                            "completion_tokens": plan_c,
                            "total_tokens": plan_t,
                        },
                        "total_tokens": plan_t,
                    }
                    steps.append(virtual_step)

                steps.sort(
                    key=lambda x: (
                        x.get("timestamp") or 0,
                        x.get("step_number") or 0,
                    )
                )

            return steps


step_repo = StepRepository()
