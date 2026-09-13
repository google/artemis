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

"""Universal (LangChain ChatModel) execution loop for the Explorer agent.

Split out of ``artemis.agents.explorer.explorer``: the ``_run_universal``
reasoning loop, packaged as a mixin consumed by ``Explorer``.
"""

import asyncio
import base64
import json
from typing import TYPE_CHECKING, Any

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from artemis.agents.explorer.geometry import is_valid_norm_point
from artemis.agents.explorer.tiers import SUBMIT_TOOL
from artemis.agents.explorer.tool_declarations import UNIVERSAL_EXPLORER_TOOLS
from artemis.graph.state import State
from artemis.services.llm import get_llm
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class UniversalRunnerMixin:
    """Universal-engine reasoning loop of :class:`Explorer`."""

    if TYPE_CHECKING:
        from artemis.context import ArtemisContext

        ctx: ArtemisContext
        denylisted_tools: set[str]

        def _hidden_tool_names(self) -> set[str]: ...

        def _enrich_candidates(self, candidates: list[Any]) -> list[Any]: ...

        async def exec_ask_perception_tool(
            self, search_query: str, nx: int, ny: int, detect_queries: list[str]
        ) -> dict[str, Any]: ...

        async def exec_detect_objects(
            self, queries: list[str], target_image_id: str = "img_0"
        ) -> dict[str, Any]: ...

        async def exec_ask_image_processor(
            self, instruction: str, target_image_id: str = "img_0"
        ) -> dict[str, Any]: ...

        async def exec_get_ocr_list(self) -> dict[str, Any]: ...

        async def exec_inspect_region(
            self, x_min: int, y_min: int, x_max: int, y_max: int, zoom_factor: float = 2.0
        ) -> dict[str, Any]: ...

    def _universal_exposed_tools(self) -> list[dict[str, Any]]:
        """OpenAI-style tool schemas the model may see (tier-hidden ones excluded)."""
        hidden = self._hidden_tool_names()
        return [t for t in UNIVERSAL_EXPLORER_TOOLS if t["function"]["name"] not in hidden]

    async def _run_universal(
        self,
        *,
        query: str,
        context_feedback: str,
        screenshot_path: str,
        state: State,
        minimal_list: str,
        prompt_template: str,
        max_turns: int,
    ) -> str:
        """Executes the Explorer reasoning loop via a universal LangChain ChatModel."""
        llm = get_llm(self.ctx, name="explorer")

        exposed_tools = self._universal_exposed_tools()
        bound_llm = llm.bind_tools(exposed_tools)

        messages = self._build_universal_messages(
            query, context_feedback, screenshot_path, minimal_list, prompt_template
        )

        turn = 0
        while turn < max_turns:
            turn += 1
            is_final_turn = turn == max_turns

            if is_final_turn:
                messages.append(
                    HumanMessage(
                        content=(
                            "[WARNING] This is your final iteration. You MUST"
                            " call 'submit_answer' to submit your final result."
                        )
                    )
                )
                submit_only_tools = [
                    t for t in exposed_tools if t["function"]["name"] == SUBMIT_TOOL
                ]
                current_llm = llm.bind_tools(submit_only_tools) if submit_only_tools else bound_llm
            else:
                current_llm = bound_llm

            response = await asyncio.wait_for(current_llm.ainvoke(messages), timeout=180)
            messages.append(response)

            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                logger.warning(f"Universal Explorer turn {turn}: No tool calls generated.")
                if is_final_turn:
                    return json.dumps(
                        {
                            "candidates": [],
                            "fallback_message": str(response.content) or "No candidates found.",
                        },
                        ensure_ascii=False,
                    )
                continue

            submit_outcome = self._universal_submit_outcome(tool_calls)
            if submit_outcome is not None:
                return submit_outcome

            await self._universal_dispatch_tools(tool_calls, messages, turn)

        return json.dumps(
            {
                "candidates": [],
                "fallback_message": "Explorer reached max iterations without finding candidates.",
            },
            ensure_ascii=False,
        )

    def _build_universal_messages(
        self,
        query: str,
        context_feedback: str,
        screenshot_path: str,
        minimal_list: str,
        prompt_template: str,
    ) -> list[BaseMessage]:
        """Builds the initial system/user message pair for the universal loop."""
        with open(screenshot_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

        user_content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"Operator Request:\n- Query: {query}\n"
                    f"- Context Feedback: {context_feedback}\n\n"
                    "Initial marked UI elements list:\n"
                    f"{minimal_list}\n"
                ),
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
            },
        ]

        messages: list[BaseMessage] = [
            SystemMessage(content=prompt_template),
            HumanMessage(content=user_content),
        ]
        return messages

    def _universal_submit_outcome(self, tool_calls: list) -> str | None:
        """Returns the validated submit_answer outcome JSON, or None if absent."""
        submit_call = next((tc for tc in tool_calls if tc.get("name") == SUBMIT_TOOL), None)
        if not submit_call:
            return None

        args = submit_call.get("args", {})
        candidates = args.get("candidates", [])
        fallback_message = args.get("fallback_message", "")

        valid_candidates = self._enrich_candidates(
            [
                cand
                for cand in candidates
                if isinstance(cand, dict) and is_valid_norm_point(cand.get("coords"))
            ]
        )

        return json.dumps(
            {"candidates": valid_candidates, "fallback_message": fallback_message},
            ensure_ascii=False,
        )

    async def _universal_execute_tool(self, name: str, args: dict) -> dict[str, Any]:
        """Runs one perception tool by name; unknown names yield an error result."""
        if name == "ask_perception_tool":
            return await self.exec_ask_perception_tool(
                search_query=args.get("search_query"),
                nx=args.get("nx"),
                ny=args.get("ny"),
                detect_queries=args.get("detect_queries"),
            )
        if name == "detect_objects":
            return await self.exec_detect_objects(
                queries=args.get("queries"),
                target_image_id=args.get("target_image_id", "img_0"),
            )
        if name == "get_ocr_list":
            return await self.exec_get_ocr_list()
        if name == "ask_image_processor":
            return await self.exec_ask_image_processor(
                instruction=args.get("instruction"),
                target_image_id=args.get("target_image_id", "img_0"),
            )
        if name == "inspect_region":
            return await self.exec_inspect_region(
                x_min=args.get("x_min"),
                y_min=args.get("y_min"),
                x_max=args.get("x_max"),
                y_max=args.get("y_max"),
                zoom_factor=args.get("zoom_factor", 2.0),
            )
        return {"text": f"Error: Tool '{name}' is not recognized."}

    @staticmethod
    def _tool_image_paths(res: Any) -> list[str]:
        """Collects the annotated image paths a tool result may carry."""
        if not isinstance(res, dict):
            return []
        paths: list[str] = []
        single = res.get("image_path")
        if single:
            paths.append(str(single))
        paths.extend(str(p) for p in (res.get("image_paths") or []) if p)
        return paths

    def _build_tool_images_message(self, images: list[tuple[str, str]]) -> HumanMessage | None:
        """One HumanMessage carrying every annotated image produced this turn.

        LangChain ``ToolMessage`` content is text-only for most providers, so
        the images ride on a follow-up human turn instead; unreadable files
        are skipped so a missing annotation never aborts the loop.
        """
        blocks: list[dict[str, Any]] = []
        tool_names: list[str] = []
        for tool_name, path in images:
            try:
                with open(path, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode("utf-8")
            except OSError as read_err:
                logger.warning(f"Skipping unreadable tool image {path}: {read_err}")
                continue
            if tool_name not in tool_names:
                tool_names.append(tool_name)
            blocks.append(
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
            )
        if not blocks:
            return None
        header = {
            "type": "text",
            "text": f"[Annotated image(s) returned by: {', '.join(tool_names)}]",
        }
        return HumanMessage(content=[header, *blocks])

    async def _universal_dispatch_tools(
        self, tool_calls: list, messages: list[BaseMessage], turn: int
    ) -> None:
        """Executes non-submit tool calls, appending ToolMessages and their images."""
        images: list[tuple[str, str]] = []
        for tc in tool_calls:
            name = tc.get("name")
            args = tc.get("args", {})
            call_id = tc.get("id", f"call_{turn}_{name}")

            if name in self.denylisted_tools:
                messages.append(
                    ToolMessage(
                        tool_call_id=call_id,
                        name=name,
                        content=f"Tool '{name}' is denylisted and unavailable.",
                    )
                )
                continue

            try:
                res = await self._universal_execute_tool(name, args)
                res_text = res.get("text") or res.get("result") or str(res)
                messages.append(ToolMessage(tool_call_id=call_id, name=name, content=res_text))
                images.extend((name, path) for path in self._tool_image_paths(res))
            except Exception as tool_err:
                logger.error(f"Error executing tool '{name}': {tool_err}")
                messages.append(
                    ToolMessage(
                        tool_call_id=call_id,
                        name=name,
                        content=f"Error executing tool '{name}': {tool_err}",
                    )
                )

        image_message = self._build_tool_images_message(images) if images else None
        if image_message is not None:
            messages.append(image_message)
