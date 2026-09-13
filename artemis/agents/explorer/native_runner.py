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

"""Native Gemini execution loop of ``Explorer.run``.

Split out of ``artemis.agents.explorer.explorer``: the native-SDK reasoning
loop and its named phases (model invocation, submit_answer interception and
validation, tool dispatching, failure mapping, resource cleanup), packaged as
a mixin consumed by ``Explorer``.  Every google-genai call goes through the
SDK's ``client.aio`` surface so uploads, cache management and generation
never block the event loop.  This module also owns
:func:`_generate_content_with_reliability`, the shared reliability wrapper
for native google-genai model calls.
"""

import asyncio
from collections.abc import Awaitable, Callable
import json
import os
import time
from typing import TYPE_CHECKING, Any

from google.genai import types

from artemis.agents.explorer.geometry import is_valid_norm_point
from artemis.agents.explorer.tiers import SUBMIT_TOOL
from artemis.constants import SAFETY_SETTINGS_BLOCK_NONE
from artemis.data_engine.trace import TraceSpan
from artemis.llm.reliability import (
    LLMExhaustedError,
    LLMPermanentError,
    classify_failure,
    retry_policy_for,
)
from artemis.services.llm import _record_llm_event, _record_llm_retry
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

#: Loads a local image as a ``Part`` (inline bytes or a File API reference).
ImagePartGetter = Callable[[str], Awaitable[types.Part]]


async def _generate_content_with_reliability(operation, *, label: str = "Explorer model call"):
    """Run one native google-genai model call under the shared reliability layer.

    ``operation`` must return a fresh awaitable on each call. Non-retryable
    failures raise ``LLMPermanentError``; exhausted retries raise
    ``LLMExhaustedError`` according to the shared category policy.
    """
    attempt = 0
    while True:
        try:
            return await operation()
        except Exception as call_err:
            attempt += 1
            failure = classify_failure(call_err)
            if not failure.retryable:
                logger.error(f"{label} permanently failed [{failure.category.value}]: {call_err}")
                _record_llm_event(
                    "llm_gave_up",
                    {
                        "source": "explorer",
                        "error": str(call_err)[:1000],
                        "category": failure.category.value,
                        "retryable": False,
                    },
                    status="failed",
                )
                raise LLMPermanentError(
                    f"{label} failed [{failure.category.value}]: {call_err}",
                    failure=failure,
                    cause=call_err,
                ) from call_err
            policy = retry_policy_for(failure.category)
            if attempt >= policy.max_attempts:
                logger.error(
                    f"{label} exhausted {attempt} attempt(s) [{failure.category.value}]: {call_err}"
                )
                _record_llm_event(
                    "llm_gave_up",
                    {
                        "source": "explorer",
                        "error": str(call_err)[:1000],
                        "category": failure.category.value,
                        "retryable": True,
                        "attempts": attempt,
                    },
                    status="failed",
                )
                raise LLMExhaustedError(
                    f"{label} exhausted {attempt} attempt(s)"
                    f" [{failure.category.value}]: {call_err}",
                    failure=failure,
                    cause=call_err,
                ) from call_err
            delay = policy.delay_for(attempt)
            logger.warning(
                f"{label} failed [{failure.category.value}] on attempt"
                f" {attempt}/{policy.max_attempts}: {call_err}."
                f" Retrying in {delay:.2f}s..."
            )
            _record_llm_retry(
                str(call_err),
                delay,
                attempt=attempt,
                max_retries=policy.max_attempts,
                source="explorer",
            )
            await asyncio.sleep(delay)


def _bare_tool_name(name: str) -> str:
    """Strips a namespace prefix (``ns:tool``) some models add to tool names."""
    return name.split(":")[-1] if ":" in name else name


class NativeRunnerMixin:
    """Native Gemini SDK reasoning loop of :class:`Explorer`."""

    # This mixin is composed into ``Explorer``.  Declaring the attributes and
    # sibling-mixin methods it consumes makes that host contract visible to
    # static analysis without adding runtime shims or changing MRO behavior.
    if TYPE_CHECKING:
        from artemis.context import ArtemisContext

        ctx: ArtemisContext
        denylisted_tools: set[str]
        turn_latencies: list[float]
        turn_cached_tokens: list[int]
        trace_history: list[dict[str, Any]]

        def get_exposed_tools(
            self, only_submit: bool = False
        ) -> list[types.FunctionDeclaration]: ...

        def _prune_historical_images(self, contents: list, keep_last: int = 1) -> None: ...

        def _enrich_candidates(self, candidates: list[Any]) -> list[Any]: ...

        async def exec_ask_perception_tool(
            self,
            search_query: str,
            nx: int,
            ny: int,
            detect_queries: list[str],
        ) -> dict[str, Any]: ...

        async def exec_detect_objects(
            self, queries: list[str], target_image_id: str = "img_0"
        ) -> dict[str, Any]: ...

        async def exec_ask_image_processor(
            self, instruction: str, target_image_id: str = "img_0"
        ) -> dict[str, Any]: ...

        async def exec_get_ocr_list(self) -> dict[str, Any]: ...

        async def exec_inspect_region(
            self,
            x_min: int,
            y_min: int,
            x_max: int,
            y_max: int,
            zoom_factor: float = 2.0,
        ) -> dict[str, Any]: ...

    def _make_image_part_getter(
        self, client, use_file_api: bool, uploaded_files: list
    ) -> ImagePartGetter:
        """Builds the image-part loader used for screenshots and tool images."""

        async def get_image_part(file_path: str) -> types.Part:
            if use_file_api:
                file_ref = await client.aio.files.upload(file=file_path)
                uploaded_files.append(file_ref)
                return types.Part(
                    file_data=types.FileData(
                        file_uri=file_ref.uri,
                        mime_type=file_ref.mime_type or "image/jpeg",
                    )
                )
            with open(file_path, "rb") as f:
                img_bytes = f.read()
            return types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")

        return get_image_part

    def _build_initial_contents(
        self, query: str, context_feedback: str, minimal_list: str, initial_part: types.Part
    ) -> list[types.Content]:
        """Builds the initial user content with the operator request and screenshot."""
        return [
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text=f"""Operator Request:
- Query: {query}
- Context Feedback: {context_feedback}

Initial marked UI elements list (corresponding to numbers ①, ②... in the image):
{minimal_list}
"""
                    ),
                    initial_part,
                ],
            )
        ]

    async def _create_cache_resource(
        self, client, model_name: str, prompt_template: str, contents: list
    ):
        """Creates the explicit cache resource; returns None on failure."""
        try:
            logger.info("Creating cache resource for Explorer...")
            cached_content = await client.aio.caches.create(
                model=model_name,
                config=types.CreateCachedContentConfig(
                    contents=[contents[0]],
                    system_instruction=prompt_template,
                    tools=[types.Tool(function_declarations=self.get_exposed_tools())],
                    tool_config=types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode=types.FunctionCallingConfigMode.ANY
                        ),
                        include_server_side_tool_invocations=True,
                    ),
                    ttl="300s",
                ),
            )
            logger.info(f"Cache resource created successfully: {cached_content.name}")
            return cached_content
        except Exception as cache_err:
            logger.error(f"Failed to create cache resource: {cache_err}")
            return None

    async def _run_native(
        self,
        *,
        client,
        query: str,
        context_feedback: str,
        minimal_list: str,
        img_to_read: str,
        enable_caching: bool,
        prompt_template: str,
        model_name: str,
        temperature,
        thinking_level,
        max_turns: int,
    ) -> str:
        """Runs the native Gemini path: cache setup, reasoning loop, cleanup."""
        use_file_api = os.getenv("ARTEMIS_USE_FILE_API", "false").lower() == "true"
        uploaded_files: list = []
        get_image_part = self._make_image_part_getter(client, use_file_api, uploaded_files)

        cached_content = None
        try:
            initial_part = await get_image_part(img_to_read)
            contents = self._build_initial_contents(
                query, context_feedback, minimal_list, initial_part
            )

            if enable_caching:
                cached_content = await self._create_cache_resource(
                    client, model_name, prompt_template, contents
                )

            agent_outcome = await self._native_loop(
                client=client,
                contents=contents,
                get_image_part=get_image_part,
                cached_content=cached_content,
                prompt_template=prompt_template,
                model_name=model_name,
                temperature=temperature,
                thinking_level=thinking_level,
                max_turns=max_turns,
            )

        except Exception as e:
            agent_outcome = self._format_native_failure(e, model_name)
        finally:
            await self._cleanup_native_resources(client, cached_content, uploaded_files)

        return agent_outcome

    async def _native_loop(
        self,
        *,
        client,
        contents: list,
        get_image_part: ImagePartGetter,
        cached_content,
        prompt_template: str,
        model_name: str,
        temperature,
        thinking_level,
        max_turns: int,
    ) -> str:
        """Main native reasoning loop: one model turn per iteration."""
        turn = 0
        agent_outcome = ""
        self.turn_latencies = []
        self.turn_cached_tokens = []
        self.trace_history = []

        while turn < max_turns:
            turn += 1
            logger.info(f"Turn {turn}/{max_turns}: Invoking Native Gemini SDK for Explorer...")

            self._prune_historical_images(contents, keep_last=1)

            is_final_turn = turn == max_turns
            if is_final_turn:
                warning_msg = (
                    "\n[WARNING] This is your final iteration. You MUST"
                    " call 'submit_answer' to submit your final result. No"
                    " other tools are available."
                )
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=warning_msg)],
                    )
                )

            response, thinking_parts, text_parts = await self._invoke_native_model(
                client=client,
                contents=contents,
                cached_content=cached_content,
                is_final_turn=is_final_turn,
                prompt_template=prompt_template,
                model_name=model_name,
                temperature=temperature,
                thinking_level=thinking_level,
            )

            turn_record = {
                "iteration": turn,
                "thoughts": ("\n".join(thinking_parts) if thinking_parts else ""),
                "tool_calls": [],
            }

            function_calls = response.function_calls
            if not function_calls:
                self._handle_missing_tool_calls(
                    contents, response, turn_record, text_parts, max_turns, turn
                )
                continue

            # Record the model's function call request in history
            contents.append(self._model_content(response, function_calls))

            tool_response_parts: list[types.Part] = []

            # Intercept and validate submit_answer to manage the task lifecycle
            submit_outcome = self._process_submit_answer(
                function_calls, turn_record, tool_response_parts
            )
            if submit_outcome is not None:
                agent_outcome = submit_outcome
                break

            await self._dispatch_tool_calls(
                function_calls, get_image_part, turn_record, tool_response_parts
            )

            if tool_response_parts:
                # Native Gemini SDK allows the 'user' role to return mixed parts
                contents.append(types.Content(role="user", parts=tool_response_parts))

            self.trace_history.append(turn_record)

        if turn >= max_turns and not agent_outcome:
            agent_outcome = (
                "Error: Explorer reached maximum iterations without a conclusive answer."
            )

        return agent_outcome

    @staticmethod
    def _model_content(response, function_calls: list) -> types.Content:
        """The model turn to record: the SDK's content when present, else a rebuild."""
        candidates = getattr(response, "candidates", None)
        if candidates:
            content = getattr(candidates[0], "content", None)
            if content:
                return content
        return types.Content(
            role="model",
            parts=[types.Part(function_call=fc) for fc in function_calls],
        )

    def _build_generate_config(
        self,
        *,
        cached_content,
        is_final_turn: bool,
        prompt_template: str,
        temperature,
        thinking_level,
    ) -> types.GenerateContentConfig:
        """Builds the per-turn generation config (cached vs full tool config)."""
        thinking_config = (
            types.ThinkingConfig(thinking_level=thinking_level) if thinking_level else None
        )
        if cached_content and not is_final_turn:
            return types.GenerateContentConfig(
                cached_content=cached_content.name,
                temperature=temperature,
                safety_settings=SAFETY_SETTINGS_BLOCK_NONE,
                thinking_config=thinking_config,
            )
        return types.GenerateContentConfig(
            system_instruction=prompt_template,
            temperature=temperature,
            safety_settings=SAFETY_SETTINGS_BLOCK_NONE,
            tools=[
                types.Tool(function_declarations=self.get_exposed_tools(only_submit=is_final_turn))
            ],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.ANY
                ),
                include_server_side_tool_invocations=True,
            ),
            thinking_config=thinking_config,
        )

    @staticmethod
    def _extract_response_texts(response) -> tuple[list[str], list[str]]:
        """Extracts model thoughts and plain text parts from a response."""
        thinking_parts = []
        text_parts = []
        candidates = getattr(response, "candidates", None)
        if not candidates and isinstance(response, dict):
            candidates = response.get("candidates")

        if candidates and len(candidates) > 0:
            candidate = candidates[0]
            content = getattr(candidate, "content", None)
            if not content and isinstance(candidate, dict):
                content = candidate.get("content")

            parts = getattr(content, "parts", None)
            if not parts and isinstance(content, dict):
                parts = content.get("parts")

            if parts:
                for part in parts:
                    is_thought = False
                    part_text = None
                    if isinstance(part, dict):
                        is_thought = part.get("thought", False)
                        part_text = part.get("text", None)
                    else:
                        is_thought = getattr(part, "thought", False)
                        part_text = getattr(part, "text", None)

                    if is_thought and part_text:
                        thinking_parts.append(part_text)
                    elif part_text:
                        text_parts.append(part_text)
        return thinking_parts, text_parts

    async def _invoke_native_model(
        self,
        *,
        client,
        contents: list,
        cached_content,
        is_final_turn: bool,
        prompt_template: str,
        model_name: str,
        temperature,
        thinking_level,
    ):
        """Invokes the model for one turn under a trace span; returns response and texts."""
        ctx = self.ctx
        start_turn = time.perf_counter()
        with TraceSpan(name="gemini_explorer_call", ctx=ctx) as span:
            generate_config = self._build_generate_config(
                cached_content=cached_content,
                is_final_turn=is_final_turn,
                prompt_template=prompt_template,
                temperature=temperature,
                thinking_level=thinking_level,
            )

            response = await _generate_content_with_reliability(
                lambda: asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=generate_config,
                    ),
                    timeout=180,
                ),
            )
            end_turn = time.perf_counter()
            turn_dur = end_turn - start_turn
            self.turn_latencies.append(turn_dur)

            cached_tokens = 0
            if getattr(response, "usage_metadata", None) and getattr(
                response.usage_metadata,
                "cached_content_token_count",
                None,
            ):
                cached_tokens = response.usage_metadata.cached_content_token_count
            self.turn_cached_tokens.append(cached_tokens)
            span.result = (
                f"Function calls: {len(response.function_calls)}"
                if response.function_calls
                else "Final answer"
            )
            # Extract and log model thoughts and text if present
            thinking_parts, text_parts = self._extract_response_texts(response)
            if thinking_parts:
                span.payload["explorer_thought"] = "\n".join(thinking_parts)
            if text_parts:
                span.payload["explorer_text"] = "\n".join(text_parts)

        return response, thinking_parts, text_parts

    def _handle_missing_tool_calls(
        self,
        contents: list,
        response,
        turn_record: dict,
        text_parts: list[str],
        max_turns: int,
        turn: int,
    ) -> None:
        """Handles a turn where the model produced plain text instead of tool calls.

        Responses without candidates (safety blocks, empty replies) are
        recorded as a synthetic model text part so the conversation history
        stays well-formed for the retry.
        """
        logger.warning(f"Explorer turn {turn}: Model hallucinated plain text. Forcing retry.")
        model_text = "\n".join(text_parts) if text_parts else ""
        turn_record["tool_calls"].append(
            {
                "name": "hallucinated_plain_text",
                "args": {"text": model_text},
                "response": {
                    "error": (
                        "Forced retry because model failed to call submit_answer or any other tool"
                    )
                },
            }
        )
        self.trace_history.append(turn_record)

        candidates = getattr(response, "candidates", None)
        model_content = getattr(candidates[0], "content", None) if candidates else None
        if not model_content:
            model_content = types.Content(
                role="model",
                parts=[types.Part.from_text(text=model_text or "(no response)")],
            )
        contents.append(model_content)
        contents.append(
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text=(
                            "You have not called 'submit_answer'"
                            " to submit your final answer yet. If"
                            " you are not certain about the final"
                            " answer, you can continue exploring"
                            f" {max_turns - turn} more"
                            " time(s)."
                        )
                    )
                ],
            )
        )

    @staticmethod
    def _validate_submit_args(function_calls: list, args: dict, candidates: list) -> list[str]:
        """Validates a submit_answer call; returns the list of validation errors."""
        errors = []

        if len(function_calls) > 1:
            errors.append(
                "The tool 'submit_answer' MUST be called"
                " individually in your final turn without any other"
                " parallel tool calls. Please remove other tool"
                " calls and re-submit."
            )

        fallback_message = args.get("fallback_message", "")
        if not candidates and not fallback_message:
            errors.append(
                "The candidates list is empty. You must provide at"
                " least one candidate matching the query, or"
                " explain observations via fallback_message."
            )

        for i, cand in enumerate(candidates):
            label = cand.get("label")
            coords = cand.get("coords")
            if not label:
                errors.append(f"Candidate at index {i} is missing a 'label'.")
            if not coords or not isinstance(coords, list) or len(coords) != 2:
                errors.append(
                    f"Candidate '{label or i}' must have 'coords'"
                    " as an array of exactly 2 integers `[nx,"
                    " ny]`."
                )
            elif not is_valid_norm_point(coords):
                try:
                    nx, ny = int(coords[0]), int(coords[1])
                except (ValueError, TypeError):
                    errors.append(f"Candidate '{label or i}' coordinates are invalid integers.")
                else:
                    errors.append(
                        f"Candidate '{label or i}' coordinates"
                        f" `[{nx}, {ny}]` must strictly be in"
                        " the `[0-1000]` normalized scale"
                        " range inclusive."
                    )

        return errors

    def _process_submit_answer(
        self, function_calls: list, turn_record: dict, tool_response_parts: list
    ) -> str | None:
        """Intercepts submit_answer; returns the final outcome JSON when valid.

        On validation failure the error is fed back as a tool response and
        None is returned so the ReAct loop can continue and self-correct.
        """
        submit_call = next(
            (fc for fc in function_calls if _bare_tool_name(fc.name) == SUBMIT_TOOL),
            None,
        )
        if not submit_call:
            return None

        args = submit_call.args or {}
        candidates = args.get("candidates", [])
        errors = self._validate_submit_args(function_calls, args, candidates)

        if errors:
            logger.warning(f"Explorer submit_answer validation failed: {errors}")
            turn_record["tool_calls"].append(
                {
                    "name": SUBMIT_TOOL,
                    "args": args,
                    "response": {"error": ("Validation Failed:\n" + "\n".join(errors))},
                }
            )
            tool_response_parts.append(
                types.Part.from_function_response(
                    name=SUBMIT_TOOL,
                    response={
                        "error": (
                            "Validation Failed:\n"
                            + "\n".join(errors)
                            + "\nPlease correct these formatting"
                            " errors and re-submit using"
                            " submit_answer."
                        )
                    },
                )
            )
            # Allow the ReAct loop to continue so the model can self-correct
            return None

        logger.info("Explorer submit_answer validation passed successfully!")
        # Candidates submitted by a registered label inherit that element's
        # bounds; the rest of the model's arguments are passed through as is.
        outcome_args = dict(args)
        outcome_args["candidates"] = self._enrich_candidates(candidates)
        agent_outcome = json.dumps(outcome_args, ensure_ascii=False)
        turn_record["tool_calls"].append(
            {"name": SUBMIT_TOOL, "args": args, "response": {"result": "success"}}
        )
        self.trace_history.append(turn_record)
        return agent_outcome

    @staticmethod
    async def _append_tool_response_with_images(
        name: str,
        res: dict,
        get_image_part: ImagePartGetter,
        tool_response_parts: list,
        *,
        result_key: str = "result",
    ) -> None:
        """Appends a tool result part plus every annotated image it produced.

        Image loading failures are logged and skipped: the textual result is
        still delivered, so one unreadable annotation cannot sink the turn.
        """
        tool_response_parts.append(
            types.Part.from_function_response(name=name, response={result_key: res.get("text")})
        )
        image_paths = [p for p in [res.get("image_path"), *(res.get("image_paths") or [])] if p]
        for img_p in image_paths:
            try:
                tool_response_parts.append(await get_image_part(img_p))
            except Exception as e:
                logger.warning(f"Failed to load image response part for {img_p}: {e}")

    async def _execute_native_tool(
        self,
        name: str,
        args: dict,
        get_image_part: ImagePartGetter,
        tool_call_trace: dict,
        tool_response_parts: list,
    ) -> None:
        """Executes one non-submit tool call and appends its response parts."""
        if name == "ask_perception_tool":
            search_query = args.get("search_query")
            nx = args.get("nx")
            ny = args.get("ny")
            detect_queries = args.get("detect_queries")
            res = await self.exec_ask_perception_tool(
                search_query=search_query if isinstance(search_query, str) else "",
                nx=nx if isinstance(nx, int) else -1,
                ny=ny if isinstance(ny, int) else -1,
                detect_queries=detect_queries if isinstance(detect_queries, list) else [],
            )
            tool_call_trace["response"] = res
            await self._append_tool_response_with_images(
                name, res, get_image_part, tool_response_parts, result_key="text"
            )
        elif name == "detect_objects":
            res = await self.exec_detect_objects(
                queries=args.get("queries") or [],
                target_image_id=args.get("target_image_id", "img_0"),
            )
            tool_call_trace["response"] = res
            await self._append_tool_response_with_images(
                name, res, get_image_part, tool_response_parts
            )
        elif name == "ask_image_processor":
            res = await self.exec_ask_image_processor(
                instruction=args.get("instruction") or "",
                target_image_id=args.get("target_image_id", "img_0"),
            )
            tool_call_trace["response"] = res
            await self._append_tool_response_with_images(
                name, res, get_image_part, tool_response_parts
            )
        elif name == "get_ocr_list":
            res = await self.exec_get_ocr_list()
            tool_call_trace["response"] = res
            await self._append_tool_response_with_images(
                name, res, get_image_part, tool_response_parts
            )
        elif name == "inspect_region":
            res = await self.exec_inspect_region(
                x_min=args["x_min"],
                y_min=args["y_min"],
                x_max=args["x_max"],
                y_max=args["y_max"],
                zoom_factor=args.get("zoom_factor", 2.0),
            )
            tool_call_trace["response"] = res
            await self._append_tool_response_with_images(
                name, res, get_image_part, tool_response_parts
            )
        else:
            tool_call_trace["response"] = {"error": f"Tool {name} not found"}
            tool_response_parts.append(
                types.Part.from_function_response(
                    name=name,
                    response={"error": f"Tool {name} not found"},
                )
            )

    async def _dispatch_tool_calls(
        self,
        function_calls: list,
        get_image_part: ImagePartGetter,
        turn_record: dict,
        tool_response_parts: list,
    ) -> None:
        """Dispatches all non-submit tool calls sequentially with tracing.

        Denylisted names are refused here as well, even though they are
        absent from the declarations, because models occasionally call tools
        they were never offered.
        """
        for fc in function_calls:
            name = _bare_tool_name(fc.name)
            if name == SUBMIT_TOOL:
                continue
            args = fc.args or {}
            tool_call_trace = {"name": name, "args": args}
            if name in self.denylisted_tools:
                logger.warning(
                    f"Explorer attempted to call denylisted tool '{name}'. Blocking execution."
                )
                error = f"Tool '{name}' is denylisted and unavailable."
                tool_call_trace["response"] = {"error": error}
                turn_record["tool_calls"].append(tool_call_trace)
                tool_response_parts.append(
                    types.Part.from_function_response(name=fc.name, response={"error": error})
                )
                continue

            logger.info(f"Explorer executing tool '{name}' sequentially...")

            try:
                await self._execute_native_tool(
                    name, args, get_image_part, tool_call_trace, tool_response_parts
                )
            except Exception as e:
                logger.error(f"Explorer tool {name} execution failed: {e}")
                tool_call_trace["response"] = {"error": str(e)}
                tool_response_parts.append(
                    types.Part.from_function_response(name=name, response={"error": str(e)})
                )
            finally:
                turn_record["tool_calls"].append(tool_call_trace)

    def _format_native_failure(self, e: Exception, model_name: str) -> str:
        """Maps a loop failure to the user-facing fallback outcome JSON."""
        logger.error(f"Explorer execution loop failed: {e}")

        err_msg = str(e)
        if "preempted" in err_msg.lower():
            clean_msg = (
                "Model request was preempted by the server due to high"
                " demand. Please try again later."
            )
        elif "overloaded" in err_msg.lower() or "503" in err_msg:
            clean_msg = "Model service is currently overloaded. Please try again later."
        elif "quota" in err_msg.lower() or "429" in err_msg:
            clean_msg = "Model API quota exceeded or rate limited."
        elif "thinkingconfig" in err_msg.lower():
            clean_msg = (
                "Model configuration error: ThinkingConfig is not"
                f" supported for model {model_name}."
            )
        else:
            clean_msg = f"Explorer agent execution failed: {err_msg}"

        return json.dumps(
            {"candidates": [], "fallback_message": clean_msg},
            ensure_ascii=False,
        )

    async def _cleanup_native_resources(self, client, cached_content, uploaded_files: list) -> None:
        """Deletes the cache resource and File API uploads created for this run."""
        if cached_content:
            try:
                logger.info(f"Deleting cache resource: {cached_content.name}")
                await client.aio.caches.delete(name=cached_content.name)
            except Exception as cleanup_cache_err:
                logger.warning(f"Failed to delete cache resource: {cleanup_cache_err}")
        for file_ref in uploaded_files:
            try:
                await client.aio.files.delete(name=file_ref.name)
            except Exception as cleanup_err:
                logger.warning(f"Failed to delete uploaded file {file_ref.name}: {cleanup_err}")
        logger.info(f"Explorer turn latencies: {self.turn_latencies}")
