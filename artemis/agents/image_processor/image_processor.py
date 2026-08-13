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

"""Universal Multi-Model Image Processor for Artemis."""

import base64
import json
from pathlib import Path
import shutil
import time
import uuid

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from artemis.config import settings
from artemis.context import ArtemisContext
from artemis.core.tool_declaration import ToolDeclaration
from artemis.data_engine.trace import TraceSpan, trace
from artemis.services.llm import get_llm
from artemis.utils.logger import get_logger
from artemis.utils.python_executor import PythonExecutor

logger = get_logger(__name__)

EXECUTE_PYTHON_TOOL = ToolDeclaration(
    name="execute_python",
    description=(
        "Executes the provided Python code within an isolated"
        " Jupyter environment and returns the resulting stdout,"
        " stderr, and traceback outputs."
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The Python code to execute",
            }
        },
        "required": ["code"],
    },
)

SUBMIT_RESULT_TOOL = ToolDeclaration(
    name="submit_result",
    description=("Submits a summary of the modifications to successfully complete the task."),
    parameters={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Summary of what you found or changed.",
            }
        },
        "required": ["summary"],
    },
)


class ImageProcessor:
    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx

    @trace(type="agent", name="image_processor")
    async def run(self, instruction: str, target_image_path: str) -> dict:
        try:
            llm = get_llm(self.ctx, name="image_processor")
        except Exception:
            llm = get_llm(self.ctx, name="operator")

        base_dir = settings.TRACES_PATH
        image_processor_dir = base_dir / "images" / "image_processor"
        image_processor_dir.mkdir(parents=True, exist_ok=True)
        intermediate_artifact_save_path = image_processor_dir / f"intermediates_{int(time.time())}"
        intermediate_artifact_save_path.mkdir(parents=True, exist_ok=True)
        intermediate_artifact_save_path_str = str(intermediate_artifact_save_path.resolve())

        max_iterations = 5

        prompt_path = Path(__file__).parent / "image_processor.md"
        prompt_template = prompt_path.read_text(encoding="utf-8")
        prompt_template = (
            prompt_template.replace("{target_image_path}", target_image_path)
            .replace("{instruction}", instruction)
            .replace(
                "{intermediate_artifact_save_path}",
                intermediate_artifact_save_path_str,
            )
            .replace("{max_iterations}", str(max_iterations))
        )

        tools_declaration = [EXECUTE_PYTHON_TOOL, SUBMIT_RESULT_TOOL]

        with open(target_image_path, "rb") as f:
            img_bytes = f.read()
        img_b64 = base64.b64encode(img_bytes).decode("utf-8")

        messages: list[BaseMessage] = [
            SystemMessage(content=prompt_template),
            HumanMessage(
                content=[
                    {"type": "text", "text": "Here is the target image. Begin writing your code."},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                    },
                ]
            ),
        ]

        img_0_path = intermediate_artifact_save_path / "img_0.jpg"
        try:
            shutil.copy2(target_image_path, img_0_path)
        except Exception as e:
            logger.error(f"Failed to copy original screenshot to intermediate pool: {e}")
            img_0_path = Path(target_image_path)

        self.intermediate_image_pool = {
            "img_0": {
                "image_id": "img_0",
                "path": str(img_0_path.resolve()),
                "turn_id": 0,
                "transform": {
                    "offset_x": 0.0,
                    "offset_y": 0.0,
                    "scale_x": 1.0,
                    "scale_y": 1.0,
                },
                "annotations": {},
            }
        }

        pool_json_path = intermediate_artifact_save_path / "intermediate_transforms.json"
        sync_pool = {
            "img_0": {
                "image_id": "img_0",
                "path": self.intermediate_image_pool["img_0"]["path"],
                "transform": self.intermediate_image_pool["img_0"]["transform"],
                "annotations": {},
            }
        }
        with open(pool_json_path, "w") as f:
            json.dump(sync_pool, f)

        executor = PythonExecutor(session_dir=image_processor_dir)
        iterations = 0
        final_result = None

        try:
            while iterations < max_iterations:
                iterations += 1
                logger.info(f"ImageProcessor Iteration {iterations}")

                # Inject newly created intermediate images from last turn
                if self.intermediate_image_pool:
                    image_blocks = []
                    last_turn_id = iterations - 1
                    for img_id, entry in self.intermediate_image_pool.items():
                        if entry["turn_id"] == last_turn_id:
                            try:
                                path = Path(entry["path"])
                                if path.exists():
                                    b64_data = base64.b64encode(path.read_bytes()).decode("utf-8")
                                    is_output = entry.get("is_output", False)
                                    label = (
                                        f"img_id: {img_id} (Output)"
                                        if is_output
                                        else f"img_id: {img_id}"
                                    )
                                    image_blocks.append(
                                        {
                                            "type": "image_url",
                                            "image_url": {
                                                "url": f"data:image/jpeg;base64,{b64_data}"
                                            },
                                        }
                                    )
                                    image_blocks.append({"type": "text", "text": f"\n{label}\n"})
                            except Exception as e:
                                logger.error(
                                    f"Failed to read intermediate image {entry['path']}: {e}"
                                )

                    if image_blocks:
                        if (
                            messages
                            and isinstance(messages[-1], HumanMessage)
                            and isinstance(messages[-1].content, list)
                        ):
                            messages[-1].content.extend(image_blocks)
                        else:
                            messages.append(HumanMessage(content=image_blocks))

                if iterations == max_iterations - 1:
                    warning_msg = (
                        "\n[WARNING] This is your next-to-last iteration. You"
                        " must complete your task and call `submit_result` now"
                        " or in the next turn, as no further code execution"
                        " will be allowed after that."
                    )
                    messages.append(HumanMessage(content=warning_msg))
                elif iterations == max_iterations:
                    warning_msg = (
                        "\n[WARNING] This is your final iteration. You MUST"
                        " call `submit_result` now to complete the task. No"
                        " other tools are available."
                    )
                    messages.append(HumanMessage(content=warning_msg))

                current_tools = tools_declaration
                if iterations == max_iterations:
                    current_tools = [t for t in tools_declaration if t.name == "submit_result"]

                bound_llm = llm.bind_tools(current_tools)

                with TraceSpan(name="image_processor_call", ctx=self.ctx):
                    response = await bound_llm.ainvoke(messages)

                tool_calls = response.tool_calls or []
                if not tool_calls:
                    break

                messages.append(response)

                submit_called = False
                for tc in tool_calls:
                    name = tc["name"].split(":")[-1] if ":" in tc["name"] else tc["name"]
                    args = tc.get("args") or {}
                    tc_id = tc.get("id") or str(uuid.uuid4())

                    if name == "submit_result":
                        final_result = args
                        submit_called = True
                        messages.append(
                            ToolMessage(
                                tool_call_id=tc_id,
                                name=name,
                                content=json.dumps({"status": "submitted"}),
                                status="success",
                            )
                        )
                        break

                    elif name == "execute_python":
                        code = args.get("code", "")
                        logger.info("Executing Python code via PythonExecutor")
                        exec_output = executor.execute(code)

                        messages.append(
                            ToolMessage(
                                tool_call_id=tc_id,
                                name=name,
                                content=exec_output,
                                status="success",
                            )
                        )

                        if pool_json_path.exists():
                            try:
                                with open(pool_json_path) as f:
                                    loaded_pool = json.load(f)
                                for img_id, entry in loaded_pool.items():
                                    if img_id not in self.intermediate_image_pool:
                                        self.intermediate_image_pool[img_id] = {
                                            "image_id": img_id,
                                            "path": entry["path"],
                                            "turn_id": iterations,
                                            "transform": entry["transform"],
                                            "is_output": entry.get("is_output", False),
                                            "annotations": entry.get("annotations", {}),
                                        }
                            except Exception as e:
                                logger.error(f"Failed to read pool_json_path: {e}")

                if submit_called:
                    break

        finally:
            executor.close()

        outputs = [
            entry
            for entry in self.intermediate_image_pool.values()
            if entry.get("is_output") is True
        ]

        if not outputs:
            sorted_keys = sorted(
                self.intermediate_image_pool.keys(),
                key=lambda k: (
                    int(k.split("_")[1])
                    if k.startswith("img_") and k.split("_")[1].isdigit()
                    else -1
                ),
            )
            if sorted_keys:
                last_key = sorted_keys[-1]
                outputs = [self.intermediate_image_pool[last_key]]

        summary = (
            final_result.get("summary", "")
            if final_result
            else "Failed to submit result within iterations"
        )

        return {"outputs": outputs, "summary": summary}
