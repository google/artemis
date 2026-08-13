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
import json
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool
from artemis.context import ArtemisContext
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


def get_step_details_tool(history_steps: list[dict[str, Any]]) -> BaseTool:
    @tool
    def get_step_details(
        start_step: int,
        end_step: int,
    ) -> str:
        """Retrieve the detailed, full-granularity information for a range of steps (inclusive).

        Use this when you need to inspect specific actions taken, operator
        thinking, or execution results.
        """
        try:
            s_step = int(start_step)
            e_step = int(end_step)
        except (ValueError, TypeError):
            return (
                f"Error: start_step and end_step must be integers, got {start_step} and {end_step}."
            )

        matched_steps = []
        for s in history_steps:
            step_num = s.get("step_number")
            if step_num is not None and s_step <= step_num <= e_step:
                details = {
                    "step_number": s.get("step_number"),
                    "relative_time": s.get("relative_time"),
                    "summary": s.get("summary"),
                    "action_taken": s.get("action_taken"),
                    "operator_raw_thinking": s.get("operator_raw_thinking"),
                    "operator_native_thinking": s.get("operator_native_thinking"),
                    "last_execution_result": s.get("last_execution_result"),
                    "interleaved_events": s.get("interleaved_events"),
                }
                matched_steps.append(details)
        if not matched_steps:
            return f"No steps found in range [{s_step}, {e_step}]."
        return json.dumps(matched_steps, indent=2, ensure_ascii=False)

    return get_step_details


def get_step_screenshot_tool(ctx: ArtemisContext, history_steps: list[dict[str, Any]]) -> BaseTool:
    @tool
    def get_step_screenshot(step_number: int) -> list[dict[str, Any]] | str:
        """Retrieve the screenshot for a specific step.

        Returns a multimodal content block containing the image.
        """
        if not ctx.data_engine:
            return "Error: DataEngine not available."

        try:
            step_num = int(step_number)
        except (ValueError, TypeError):
            return f"Error: step_number must be an integer, got {step_number}."

        # Find the step to get the image name
        step_record = next((s for s in history_steps if s.get("step_number") == step_num), None)
        if not step_record:
            return f"Error: Step {step_num} not found in history."

        image_name = step_record.get("pre_image_name") or step_record.get("post_image_name")
        if not image_name:
            return f"Error: No screenshot recorded for step {step_num}."

        images_dir = Path(ctx.data_engine.global_base_dir) / "images"
        image_path = images_dir / f"{image_name}.jpg"

        if not image_path.exists():
            return f"Error: Screenshot file for step {step_num} does not exist on disk."

        try:
            with open(image_path, "rb") as f:
                img_base64 = base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            return f"Error reading screenshot file: {e}"

        # Return a LangChain multimodal content structure
        return [
            {"type": "text", "text": f"Screenshot for step {step_num}:"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"},
            },
        ]

    return get_step_screenshot


def get_search_history_tool(ctx: ArtemisContext) -> BaseTool:
    @tool
    def search_history_for_text(query: str) -> str:
        """Search for a specific text query within the OCR and UI hierarchy of all historical steps."""
        if not ctx.data_engine:
            return "Error: DataEngine not available."

        session_id = ctx.data_engine.current_session_id
        if not session_id:
            return "Error: No active session."

        try:
            steps = ctx.data_engine.storage.get_steps(session_id)
        except Exception as e:
            return f"Error retrieving steps from storage: {e}"

        results = []
        for step in steps:
            step_matches = []
            # Search pre-action image
            if step.pre_image_name:
                try:
                    matches = ctx.data_engine.storage.search_ui_by_hash(step.pre_image_name, query)
                    if matches:
                        step_matches.extend(matches)
                except Exception as e:
                    logger.error(f"Failed to search UI by hash {step.pre_image_name}: {e}")

            # Search post-action image
            if step.post_image_name:
                try:
                    matches = ctx.data_engine.storage.search_ui_by_hash(step.post_image_name, query)
                    if matches:
                        step_matches.extend(matches)
                except Exception as e:
                    logger.error(f"Failed to search UI by hash {step.post_image_name}: {e}")

            if step_matches:
                # Clean up match details to not bloat context, keep top 3
                clean_matches = []
                for m in step_matches[:3]:
                    clean_matches.append(
                        {
                            "type": m.get("type"),
                            "matched_text": m.get("matched_text"),
                            "score": m.get("score"),
                        }
                    )
                results.append({"step_number": step.step_number, "matches": clean_matches})

        if not results:
            return f"No matches found for '{query}' in history."
        return json.dumps(results, indent=2, ensure_ascii=False)

    return search_history_for_text
