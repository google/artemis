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
import glob
import json
import os
from pathlib import Path
import re
from typing import Any, Literal

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, create_model

from artemis.agents.explorer.constants import EXPLORE_DESCRIPTIONS
from artemis.agents.explorer.explorer import Explorer
from artemis.agents.object_detector.object_detector import _run_object_detection
from artemis.config import ExplorerVersion, resolve_explorer_version, settings
from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace, trace_langchain_tool
from artemis.drivers.base import BaseDeviceDriver
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolCategory, ToolRegistry
from artemis.tools.tool_wrapper import ToolWrapper
from artemis.tools.types import CyFunctionDetector
from artemis.utils.logger import get_logger
from artemis.utils.visualization import draw_dots


class AskExplorerArgs(BaseModel):
    """Arguments schema for invoking the explorer subagent."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    query: str = Field(
        ...,
        description=("The target element or information to search for, including descriptions."),
    )
    context_feedback: str = Field(
        "",
        description=("Feedback from previous failed attempts or specific instructions"),
    )
    version: Literal["flash", "pro", "ultra"] | None = Field(
        default=None,
        description="Explicit explorer version to use (flash, pro, or ultra).",
    )


logger = get_logger(__name__)


class AskExplorerTool(ArtemisTool):
    """Universal tool for visually parsing and locating UI elements."""

    def __init__(
        self,
        version: ExplorerVersion | None = None,
        agent_name: str = "operator",
        description: str | None = None,
        category: ToolCategory = "explorer",
    ):
        self.version = version
        self.agent_name = agent_name
        resolved_v = version or "pro"
        explore_info = EXPLORE_DESCRIPTIONS.get(resolved_v, EXPLORE_DESCRIPTIONS["pro"])
        desc = description or explore_info["description"]
        if "{max_iterations}" in desc:
            max_iterations = 8 if resolved_v == "ultra" else 3
            desc = desc.format(max_iterations=max_iterations)

        super().__init__(
            name="ask_explorer",
            description=desc,
            args_schema=AskExplorerArgs,
            category=category,
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        query: str = "",
        context_feedback: str = "",
        version: Literal["flash", "pro", "ultra"] | None = None,
        tool_call_id: str | None = None,  # pylint: disable=unused-argument
        state: State | None = None,
        **kwargs: Any,
    ) -> Any:
        q = query or kwargs.get("Query") or ""
        cf = context_feedback or kwargs.get("ContextFeedback") or ""
        v = version or self.version
        resolved_v = (
            resolve_explorer_version(ctx, explicit_version=v, agent_or_profile_name=self.agent_name)
            if ctx
            else (v or "pro")
        )
        return await _run_explorer_logic(
            ctx=ctx,
            state=state,
            query=q,
            context_feedback=cf,
            version=resolved_v,
        )


# Universal tool instance & aliases
ask_explorer = AskExplorerTool()
AskExplorer = AskExplorerTool
AskExplorerToolAlias = AskExplorerTool
ToolRegistry.register(ask_explorer)


def get_ask_explorer_tool(
    ctx: ArtemisContext,
    version: ExplorerVersion | None = None,
    agent_name: str = "operator",
) -> BaseTool:
    """Exports ask_explorer as a LangChain BaseTool with dynamic schema."""
    resolved_version = resolve_explorer_version(
        ctx, explicit_version=version, agent_or_profile_name=agent_name
    )

    explore_info = EXPLORE_DESCRIPTIONS.get(resolved_version, EXPLORE_DESCRIPTIONS["pro"])
    description = explore_info["description"]
    if "{max_iterations}" in description:
        max_iterations = 8 if resolved_version == "ultra" else 3
        description = description.format(max_iterations=max_iterations)
    query_description = explore_info["query_description"]

    # Dynamically build argument schema so Operator does not see
    # context_feedback when flash is active
    fields = {
        "query": (str, Field(..., description=query_description)),
    }
    if resolved_version != "flash":
        fields["context_feedback"] = (
            str,
            Field(
                "",
                description=("Feedback from previous failed attempts or specific instructions"),
            ),
        )

    # pylint: disable=invalid-name
    DynamicAskExplorerArgs = create_model("AskExplorerArgs", **fields)

    tool_instance = AskExplorerTool(
        version=resolved_version,
        agent_name=agent_name,
        description=description,
    )
    tool_instance.args_schema = DynamicAskExplorerArgs

    return trace_langchain_tool(tool_instance.to_langchain_tool(ctx), ctx)


# pylint: disable=too-many-branches,too-many-locals,too-many-statements
async def _run_explorer_logic(
    ctx: ArtemisContext,
    state: State,
    query: str,
    context_feedback: str,
    version: Literal["flash", "pro", "ultra"],
) -> Any:
    if version == "flash":
        logger.info(
            "Explorer [version='flash'] intercepted -> Direct one-shot object"
            " detection (context_feedback ignored)."
        )
        screenshot_path = state.latest_screenshot
        if not screenshot_path:
            return "Explorer (Flash) error: No latest_screenshot available in state."

        queries = [q.strip() for q in query.split("|") if q.strip()]
        if not queries:
            queries = [query.strip()]

        try:
            prompt_path = Path(__file__).parent.parent.joinpath(
                "agents", "object_detector", "object_detector.json"
            )
            with open(prompt_path, encoding="utf-8") as f:
                cfg = json.load(f)
            templates = [f"{t}\n\n{cfg.get('instructions', '')}" for t in cfg.get("templates", [])]

            result_dict = await _run_object_detection(
                ctx, screenshot_path, queries, templates, global_timeout=15.0
            )
            detected_items = result_dict.get("detected", [])
            candidates = []
            for item in detected_items:
                if isinstance(item, dict) and "point" in item and len(item["point"]) == 2:
                    nx, ny = item["point"]
                    lbl = item.get("label", query)
                    candidates.append(
                        {
                            "label": lbl,
                            "coords": [int(nx), int(ny)],
                            "description": f"{lbl} (flash detection)",
                        }
                    )

            if not candidates:
                failed_queries = result_dict.get("failed", queries)
                explorer_output = json.dumps(
                    {
                        "candidates": [],
                        "fallback_message": (
                            "Flash one-shot detection could not locate:"
                            f" {', '.join(failed_queries)}. Consider checking"
                            " visual visibility or layout."
                        ),
                    }
                )
            else:
                explorer_output = json.dumps({"candidates": candidates})

        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f"Flash one-shot routing failed: {e}")
            explorer_output = json.dumps(
                {
                    "candidates": [],
                    "fallback_message": f"Flash detection error: {e}",
                }
            )
    else:

        @trace(type="agent", name="explorer", ctx=ctx)
        async def run_explorer_agent(q: str, cf: str, screenshot_path: str, state: State) -> str:
            agent = Explorer(ctx)
            env_cache = os.getenv("ARTEMIS_EXPLORER_CACHING", "").lower()
            if env_cache in ["true", "false"]:
                enable_caching = env_cache == "true"
            else:
                agent_cfg = getattr(ctx, "agent_config", None)
                if (
                    agent_cfg
                    and hasattr(agent_cfg, "explorer")
                    and hasattr(agent_cfg.explorer, "caching")
                ):
                    enable_caching = agent_cfg.explorer.caching
                elif (
                    ctx and ctx.execution_setup and hasattr(ctx.execution_setup, "explorer_caching")
                ):
                    enable_caching = ctx.execution_setup.explorer_caching
                else:
                    enable_caching = getattr(settings, "EXPLORER_CACHING", True)
            return await agent.run(
                q,
                cf,
                screenshot_path,
                state,
                enable_caching=enable_caching,
                version=version,
            )

        screenshot_path = state.latest_screenshot
        explorer_output = await run_explorer_agent(query, context_feedback, screenshot_path, state)

    try:
        data = json.loads(explorer_output)
        candidates = data.get("candidates", [])
        fallback_message = data.get("fallback_message", "")

        if candidates:
            # 1. Read Operator's active coordinates list
            indexed_points = getattr(state, "indexed_points", None)
            if indexed_points is None:
                indexed_points = []
                state.indexed_points = indexed_points

            indexed_elements = getattr(state, "indexed_elements", None)
            if indexed_elements is None:
                indexed_elements = []
                state.indexed_elements = indexed_elements

            width = 1080
            height = 2400
            operator_raw_data = getattr(state, "operator_raw_data", {}) or {}
            w_raw = operator_raw_data.get("width")
            h_raw = operator_raw_data.get("height")
            if isinstance(w_raw, int) and isinstance(h_raw, int):
                width = w_raw
                height = h_raw
            else:
                if ctx.device and getattr(ctx.device, "device_width", None):
                    width = ctx.device.device_width
                if ctx.device and getattr(ctx.device, "device_height", None):
                    height = ctx.device.device_height

            registered_info = []
            for cand in candidates:
                coords = cand.get("coords")
                desc = cand.get("description", "Explorer candidate")
                if coords and len(coords) == 2:
                    nx, ny = coords
                    pixel_x = int(max(0, min(width, nx * width / 1000)))
                    pixel_y = int(max(0, min(height, ny * height / 1000)))

                    indexed_points.append([pixel_x, pixel_y])
                    new_idx = len(indexed_points)

                    # Update indexed_elements in sync
                    indexed_elements.append(
                        {
                            "index": new_idx,
                            "center": [pixel_x, pixel_y],
                            "text": desc,
                            "bounds": None,
                            "class": "ExplorerCandidate",
                            "resource_id": None,
                            "is_ocr": False,
                        }
                    )

                    registered_info.append(f"- [{new_idx}] '{desc}' at coordinate {coords}")

            response_msg = (
                "Explorer successfully located the following candidate(s):\n"
                + "\n".join(registered_info)
            )
            response_msg += (
                "\nYou can click/act on them directly by calling perform_action"
                " with their respective index."
            )

            if fallback_message:
                response_msg += f"\n\nAdditional Notes from Explorer: {fallback_message}"

            # 2. Generate annotated image with ALL points

            base_dir = (
                Path(ctx.data_engine.base_dir)
                if ctx.data_engine and getattr(ctx.data_engine, "base_dir", None)
                else settings.TRACES_PATH
            )
            images_dir = base_dir / "images"
            explorer_dir = images_dir / "explorer_tool"
            explorer_dir.mkdir(parents=True, exist_ok=True)

            image_name = "explorer_output"
            existing_files = glob.glob(str(explorer_dir / f"{image_name}_*.jpg"))
            max_seq = 0
            for f in existing_files:
                match = re.search(r"_(\d+)\.jpg$", f)
                if match:
                    max_seq = max(max_seq, int(match.group(1)))
            seq = max_seq + 1
            output_path = explorer_dir / f"{image_name}_{seq}.jpg"

            all_labels = [str(i) for i in range(1, len(indexed_points) + 1)]

            try:
                draw_dots(
                    screenshot_path,
                    indexed_points,
                    all_labels,
                    str(output_path),
                )
                with open(output_path, "rb") as im_f:
                    img_bytes = im_f.read()
                img_b64 = base64.b64encode(img_bytes).decode("utf-8")

                return [
                    {
                        "type": "text",
                        "text": f"Explorer replied:\n{response_msg}",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                    },
                ]
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error(f"Failed to draw dots or read image in explorer_tool: {e}")
                # Fallback to pure text if drawing fails
                return f"Explorer replied:\n{response_msg}"

        elif fallback_message:
            return f"Explorer could not locate the element. Message: {fallback_message}"

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error(f"Failed to parse explorer output: {e}")
        return f"Explorer failed to provide a valid format. Raw output: {explorer_output}"

    return f"Explorer replied:\n{explorer_output}"


ask_explorer_wrapper = ToolWrapper(
    tool_fn_getter=get_ask_explorer_tool,
    on_success_fn=lambda output: f"Explorer replied:\n{output}",
    on_failure_fn=lambda error: f"Explorer failed: {error}",
)
