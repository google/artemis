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
reasoning loop, packaged as a mixin consumed by ``Explorer``.  Patched
collaborators (``get_llm``, ``is_ocr_configured``, ``logger``,
``UNIVERSAL_EXPLORER_TOOLS``) are resolved through the facade module at call
time; see ``artemis.agents.explorer._facade``.
"""

import asyncio
import base64
import json
from typing import Any

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from artemis.agents.explorer._facade import facade
from artemis.graph.state import State


class UniversalRunnerMixin:
    """Universal-engine reasoning loop of :class:`Explorer`."""

    async def _run_universal(
        self,
        query: str,
        context_feedback: str,
        screenshot_path: str,
        state: State,
        minimal_list: str,
        version: str,
        prompt_template: str,
        max_iterations: int,
    ) -> str:
        """Executes Explorer reasoning loop via Universal LangChain ChatModel."""
        _ex = facade()
        llm = _ex.get_llm(self.ctx, name="explorer")

        # Filter universal tools based on denylist and OCR configuration
        denylisted = set(self.denylisted_tools)
        if not _ex.is_ocr_configured():
            denylisted.add("get_ocr_list")
        exposed_tools = [
            t for t in _ex.UNIVERSAL_EXPLORER_TOOLS if t["function"]["name"] not in denylisted
        ]
        bound_llm = llm.bind_tools(exposed_tools)

        messages = self._build_universal_messages(
            query, context_feedback, screenshot_path, minimal_list, prompt_template
        )

        iterations = 0
        agent_outcome = ""

        while iterations < max_iterations:
            iterations += 1
            is_final_turn = iterations == max_iterations

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
                    t for t in exposed_tools if t["function"]["name"] == "submit_answer"
                ]
                current_llm = llm.bind_tools(submit_only_tools) if submit_only_tools else bound_llm
            else:
                current_llm = bound_llm

            response = await asyncio.wait_for(current_llm.ainvoke(messages), timeout=180)
            messages.append(response)

            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                _ex.logger.warning(
                    f"Universal Explorer iteration {iterations}: No tool calls generated."
                )
                if is_final_turn:
                    return json.dumps(
                        {
                            "candidates": [],
                            "fallback_message": str(response.content) or "No candidates found.",
                        },
                        ensure_ascii=False,
                    )
                continue

            # Check for submit_answer
            submit_outcome = self._universal_submit_outcome(tool_calls)
            if submit_outcome is not None:
                agent_outcome = submit_outcome
                return agent_outcome

            # Execute non-submit tools
            await self._universal_dispatch_tools(tool_calls, messages, iterations)

        if not agent_outcome:
            agent_outcome = json.dumps(
                {
                    "candidates": [],
                    "fallback_message": (
                        "Explorer reached max iterations without finding candidates."
                    ),
                },
                ensure_ascii=False,
            )

        return agent_outcome

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
        submit_call = next((tc for tc in tool_calls if tc.get("name") == "submit_answer"), None)
        if not submit_call:
            return None

        args = submit_call.get("args", {})
        candidates = args.get("candidates", [])
        fallback_message = args.get("fallback_message", "")

        # Validate coordinates
        valid_candidates = []
        for cand in candidates:
            if isinstance(cand, dict) and "coords" in cand:
                coords = cand["coords"]
                if isinstance(coords, list) and len(coords) == 2:
                    try:
                        nx, ny = int(coords[0]), int(coords[1])
                        if 0 <= nx <= 1000 and 0 <= ny <= 1000:
                            valid_candidates.append(cand)
                    except (ValueError, TypeError):
                        pass

        return json.dumps(
            {
                "candidates": valid_candidates,
                "fallback_message": fallback_message,
            },
            ensure_ascii=False,
        )

    async def _universal_dispatch_tools(
        self, tool_calls: list, messages: list[BaseMessage], iterations: int
    ) -> None:
        """Executes non-submit tool calls and appends their ToolMessages."""
        _ex = facade()
        for tc in tool_calls:
            name = tc.get("name")
            args = tc.get("args", {})
            call_id = tc.get("id", f"call_{iterations}_{name}")

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
                if name == "ask_perception_tool":
                    res = await self.exec_ask_perception_tool(
                        search_query=args.get("search_query"),
                        nx=args.get("nx"),
                        ny=args.get("ny"),
                        detect_queries=args.get("detect_queries"),
                    )
                elif name == "detect_objects":
                    res = await self.exec_detect_objects(
                        queries=args.get("queries"),
                        target_image_id=args.get("target_image_id", "img_0"),
                    )
                elif name == "get_ocr_list":
                    res = await self.exec_get_ocr_list()
                elif name == "ask_image_processor":
                    res = await self.exec_ask_image_processor(
                        instruction=args.get("instruction"),
                        target_image_id=args.get("target_image_id", "img_0"),
                    )
                elif name == "inspect_region":
                    res = await self.exec_inspect_region(
                        x_min=args.get("x_min"),
                        y_min=args.get("y_min"),
                        x_max=args.get("x_max"),
                        y_max=args.get("y_max"),
                        zoom_factor=args.get("zoom_factor", 2.0),
                    )
                else:
                    res = {"text": f"Error: Tool '{name}' is not recognized."}

                res_text = res.get("text") or res.get("result") or str(res)
                messages.append(ToolMessage(tool_call_id=call_id, name=name, content=res_text))

            except Exception as tool_err:
                _ex.logger.error(f"Error executing tool '{name}': {tool_err}")
                messages.append(
                    ToolMessage(
                        tool_call_id=call_id,
                        name=name,
                        content=f"Error executing tool '{name}': {tool_err}",
                    )
                )
