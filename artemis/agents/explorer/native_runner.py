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
a mixin consumed by ``Explorer``.  Patched collaborators (``settings``,
``logger``, ``_generate_content_with_reliability``) are resolved through the
facade module at call time; see ``artemis.agents.explorer._facade``.
"""

import asyncio
import json
import os
import time

from google.genai import types

from artemis.agents.explorer._facade import facade
from artemis.constants import SAFETY_SETTINGS_BLOCK_NONE
from artemis.data_engine.trace import TraceSpan


class NativeRunnerMixin:
    """Native Gemini SDK reasoning loop of :class:`Explorer`."""

    def _make_image_part_getter(self, client, use_file_api: bool, uploaded_files: list):
        """Builds the image-part loader used for screenshots and tool images."""

        def get_image_part(file_path: str) -> types.Part:
            if use_file_api:
                file_ref = client.files.upload(file=file_path)
                uploaded_files.append(file_ref)
                return types.Part(
                    file_data=types.FileData(
                        file_uri=file_ref.uri,
                        mime_type=file_ref.mime_type or "image/jpeg",
                    )
                )
            else:
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

    def _resolve_caching_flag(self, enable_caching):
        """Resolves the effective caching flag from env or settings when unset."""
        _ex = facade()
        if enable_caching is None:
            env_cache = os.getenv("ARTEMIS_EXPLORER_CACHING", "").lower()
            if env_cache in ["true", "false"]:
                enable_caching = env_cache == "true"
            else:
                enable_caching = getattr(_ex.settings, "EXPLORER_CACHING", True)
        return enable_caching

    def _create_cache_resource(
        self, client, model_name: str, prompt_template: str, contents: list
    ):
        """Creates the explicit cache resource; returns None on failure."""
        _ex = facade()
        cached_content = None
        try:
            _ex.logger.info("Creating cache resource for Explorer...")
            cached_content = client.caches.create(
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
            _ex.logger.info(f"Cache resource created successfully: {cached_content.name}")
        except Exception as cache_err:
            _ex.logger.error(f"Failed to create cache resource: {cache_err}")
            cached_content = None
        return cached_content

    async def _run_native(
        self,
        *,
        client,
        query: str,
        context_feedback: str,
        minimal_list: str,
        img_to_read: str,
        enable_caching,
        prompt_template: str,
        model_name: str,
        temperature,
        thinking_level,
        max_iterations: int,
    ) -> str:
        """Runs the native Gemini path: cache setup, reasoning loop, cleanup."""
        use_file_api = os.getenv("ARTEMIS_USE_FILE_API", "false").lower() == "true"
        uploaded_files = []
        get_image_part = self._make_image_part_getter(client, use_file_api, uploaded_files)

        cached_content = None
        try:
            # Upload initial screenshot
            initial_part = get_image_part(img_to_read)
            contents = self._build_initial_contents(
                query, context_feedback, minimal_list, initial_part
            )

            # Caching initialization
            enable_caching = self._resolve_caching_flag(enable_caching)

            cached_content = None
            if enable_caching:
                cached_content = self._create_cache_resource(
                    client, model_name, prompt_template, contents
                )

            # 6. Execution Loop (Native SDK Tools Dispatching)
            agent_outcome = await self._native_loop(
                client=client,
                contents=contents,
                get_image_part=get_image_part,
                cached_content=cached_content,
                prompt_template=prompt_template,
                model_name=model_name,
                temperature=temperature,
                thinking_level=thinking_level,
                max_iterations=max_iterations,
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
        get_image_part,
        cached_content,
        prompt_template: str,
        model_name: str,
        temperature,
        thinking_level,
        max_iterations: int,
    ) -> str:
        """Main native reasoning loop: one model turn per iteration."""
        _ex = facade()
        iterations = 0
        agent_outcome = ""
        self.turn_latencies = []
        self.turn_cached_tokens = []
        self.trace_history = []

        while iterations < max_iterations:
            iterations += 1
            _ex.logger.info(f"Iteration {iterations}: Invoking Native Gemini SDK for Explorer...")

            self._prune_historical_images(contents, keep_last=1)

            is_final_turn = iterations == max_iterations
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
                "iteration": iterations,
                "thoughts": ("\n".join(thinking_parts) if thinking_parts else ""),
                "tool_calls": [],
            }

            function_calls = response.function_calls
            if not function_calls:
                self._handle_missing_tool_calls(
                    contents, response, turn_record, text_parts, max_iterations, iterations
                )
                continue

            # Record model's function call request in history
            if response.candidates and response.candidates[0].content:
                contents.append(response.candidates[0].content)
            else:
                contents.append(
                    types.Content(
                        role="model",
                        parts=[types.Part(function_call=fc) for fc in function_calls],
                    )
                )

            # Execute function calls
            tool_response_parts = []

            # 6.1 Intercept and validate submit_answer to manage task lifecycle
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
                # Native Gemini SDK allows 'tool' role to return mixed parts
                contents.append(types.Content(role="user", parts=tool_response_parts))

            self.trace_history.append(turn_record)

        if iterations >= max_iterations and not agent_outcome:
            agent_outcome = (
                "Error: Explorer reached maximum iterations without a conclusive answer."
            )

        return agent_outcome

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
        thinking_kwargs = (
            {"thinking_config": types.ThinkingConfig(thinking_level=thinking_level)}
            if thinking_level
            else {}
        )
        if cached_content and not is_final_turn:
            return types.GenerateContentConfig(
                cached_content=cached_content.name,
                temperature=temperature,
                safety_settings=SAFETY_SETTINGS_BLOCK_NONE,
                **thinking_kwargs,
            )
        else:
            return types.GenerateContentConfig(
                system_instruction=prompt_template,
                temperature=temperature,
                safety_settings=SAFETY_SETTINGS_BLOCK_NONE,
                tools=[
                    types.Tool(
                        function_declarations=self.get_exposed_tools(only_submit=is_final_turn)
                    )
                ],
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode=types.FunctionCallingConfigMode.ANY
                    ),
                    include_server_side_tool_invocations=True,
                ),
                **thinking_kwargs,
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
        _ex = facade()
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

            response = await _ex._generate_content_with_reliability(
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
        max_iterations: int,
        iterations: int,
    ) -> None:
        """Handles a turn where the model produced plain text instead of tool calls."""
        _ex = facade()
        _ex.logger.warning(
            f"Explorer iteration {iterations}: Model hallucinated"
            " plain text. Forcing retry."
        )
        turn_record["tool_calls"].append(
            {
                "name": "hallucinated_plain_text",
                "args": {"text": "\n".join(text_parts) if text_parts else ""},
                "response": {
                    "error": (
                        "Forced retry because model failed to call"
                        " submit_answer or any other tool"
                    )
                },
            }
        )
        self.trace_history.append(turn_record)
        contents.append(response.candidates[0].content)
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
                            f" {max_iterations - iterations} more"
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
            else:
                try:
                    nx, ny = int(coords[0]), int(coords[1])
                    if not (0 <= nx <= 1000) or not (0 <= ny <= 1000):
                        errors.append(
                            f"Candidate '{label or i}' coordinates"
                            f" `[{nx}, {ny}]` must strictly be in"
                            " the `[0-1000]` normalized scale"
                            " range inclusive."
                        )
                except (ValueError, TypeError, IndexError):
                    errors.append(
                        f"Candidate '{label or i}' coordinates are invalid integers."
                    )

        return errors

    def _process_submit_answer(
        self, function_calls: list, turn_record: dict, tool_response_parts: list
    ) -> str | None:
        """Intercepts submit_answer; returns the final outcome JSON when valid.

        On validation failure the error is fed back as a tool response and
        None is returned so the ReAct loop can continue and self-correct.
        """
        _ex = facade()
        submit_call = next(
            (
                fc
                for fc in function_calls
                if (fc.name.split(":")[-1] if ":" in fc.name else fc.name)
                == "submit_answer"
            ),
            None,
        )
        if not submit_call:
            return None

        args = submit_call.args or {}
        candidates = args.get("candidates", [])
        errors = self._validate_submit_args(function_calls, args, candidates)

        if errors:
            _ex.logger.warning(f"Explorer submit_answer validation failed: {errors}")
            turn_record["tool_calls"].append(
                {
                    "name": "submit_answer",
                    "args": args,
                    "response": {"error": ("Validation Failed:\n" + "\n".join(errors))},
                }
            )
            tool_response_parts.append(
                types.Part.from_function_response(
                    name="submit_answer",
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
            # Allow ReAct loop to continue so LLM can self-correct and re-submit
            return None
        else:
            _ex.logger.info("Explorer submit_answer validation passed successfully!")
            agent_outcome = json.dumps(args, ensure_ascii=False)
            turn_record["tool_calls"].append(
                {
                    "name": "submit_answer",
                    "args": args,
                    "response": {"result": "success"},
                }
            )
            self.trace_history.append(turn_record)
            return agent_outcome

    @staticmethod
    def _append_tool_response_with_image(
        name: str, res: dict, get_image_part, tool_response_parts: list
    ) -> None:
        """Appends a tool result part plus its annotated image when present."""
        if res.get("image_path"):
            tool_response_parts.append(
                types.Part.from_function_response(
                    name=name,
                    response={
                        "result": res.get("text"),
                    },
                )
            )
            tool_response_parts.append(get_image_part(res["image_path"]))
        else:
            tool_response_parts.append(
                types.Part.from_function_response(
                    name=name,
                    response={"result": res.get("text")},
                )
            )

    async def _execute_native_tool(
        self,
        name: str,
        args: dict,
        get_image_part,
        tool_call_trace: dict,
        tool_response_parts: list,
    ) -> None:
        """Executes one non-submit tool call and appends its response parts."""
        _ex = facade()
        if name == "ask_perception_tool":
            res = await self.exec_ask_perception_tool(
                search_query=args.get("search_query"),
                nx=args.get("nx"),
                ny=args.get("ny"),
                detect_queries=args.get("detect_queries"),
            )
            tool_call_trace["response"] = res
            tool_response_parts.append(
                types.Part.from_function_response(
                    name=name,
                    response={
                        "text": res.get("text"),
                    },
                )
            )
            if res.get("image_paths"):
                for img_p in res["image_paths"]:
                    try:
                        tool_response_parts.append(get_image_part(img_p))
                    except Exception as e:
                        _ex.logger.warning(
                            f"Failed to load image response part for {img_p}: {e}"
                        )

        elif name == "detect_objects":
            res = await self.exec_detect_objects(
                queries=args.get("queries"),
                target_image_id=args.get("target_image_id", "img_0"),
            )
            tool_call_trace["response"] = res
            self._append_tool_response_with_image(name, res, get_image_part, tool_response_parts)

        elif name == "ask_image_processor":
            res = await self.exec_ask_image_processor(
                instruction=args.get("instruction"),
                target_image_id=args.get("target_image_id", "img_0"),
            )
            tool_call_trace["response"] = res

            tool_response_parts.append(
                types.Part.from_function_response(
                    name=name,
                    response={"result": res.get("text")},
                )
            )
            if res.get("image_paths"):
                for img_p in res["image_paths"]:
                    try:
                        tool_response_parts.append(get_image_part(img_p))
                    except Exception as e:
                        _ex.logger.warning(
                            "Failed to upload image path"
                            f" {img_p} for ask_image_processor"
                            f" tool response: {e}"
                        )

        elif name == "get_ocr_list":
            res = await self.exec_get_ocr_list()
            tool_call_trace["response"] = res
            self._append_tool_response_with_image(name, res, get_image_part, tool_response_parts)

        elif name == "inspect_region":
            res = await self.exec_inspect_region(
                x_min=args.get("x_min"),
                y_min=args.get("y_min"),
                x_max=args.get("x_max"),
                y_max=args.get("y_max"),
                zoom_factor=args.get("zoom_factor"),
            )
            tool_call_trace["response"] = res
            self._append_tool_response_with_image(name, res, get_image_part, tool_response_parts)
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
        get_image_part,
        turn_record: dict,
        tool_response_parts: list,
    ) -> None:
        """Dispatches all non-submit tool calls sequentially with tracing."""
        _ex = facade()
        for fc in function_calls:
            name = fc.name.split(":")[-1] if ":" in fc.name else fc.name
            if name == "submit_answer":
                continue
            args = fc.args or {}
            tool_call_trace = {
                "name": name,
                "args": args,
            }
            if name in self.denylisted_tools:
                _ex.logger.warning(
                    "Explorer attempted to call denylisted tool"
                    f" '{name}'. Blocking execution."
                )
                tool_call_trace["response"] = {
                    "error": (f"Tool '{name}' is denylisted and unavailable.")
                }
                turn_record["tool_calls"].append(tool_call_trace)
                tool_response_parts.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response={
                            "error": (f"Tool '{name}' is denylisted and unavailable.")
                        },
                    )
                )
                continue

            _ex.logger.info(f"Explorer executing tool '{name}' sequentially...")

            try:
                await self._execute_native_tool(
                    name, args, get_image_part, tool_call_trace, tool_response_parts
                )
            except Exception as e:
                _ex.logger.error(f"Explorer tool {name} execution failed: {e}")
                tool_call_trace["response"] = {"error": str(e)}
                tool_response_parts.append(
                    types.Part.from_function_response(name=name, response={"error": str(e)})
                )
            finally:
                turn_record["tool_calls"].append(tool_call_trace)

    def _format_native_failure(self, e: Exception, model_name: str) -> str:
        """Maps a loop failure to the user-facing fallback outcome JSON."""
        _ex = facade()
        _ex.logger.error(f"Explorer execution loop failed: {e}")

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

    async def _cleanup_native_resources(
        self, client, cached_content, uploaded_files: list
    ) -> None:
        """Closes the HTTP client and deletes cache/file resources."""
        _ex = facade()
        if self.http_client:
            try:
                await self.http_client.aclose()
                _ex.logger.info("Closed Explorer HTTP client.")
            except Exception as close_err:
                _ex.logger.warning(f"Failed to close Explorer HTTP client: {close_err}")
        if cached_content:
            try:
                _ex.logger.info(f"Deleting cache resource: {cached_content.name}")
                client.caches.delete(name=cached_content.name)
            except Exception as cleanup_cache_err:
                _ex.logger.warning(f"Failed to delete cache resource: {cleanup_cache_err}")
        for file_ref in uploaded_files:
            try:
                client.files.delete(name=file_ref.name)
            except Exception as cleanup_err:
                _ex.logger.warning(f"Failed to delete uploaded file {file_ref.name}: {cleanup_err}")
        _ex.logger.info(f"Explorer turn latencies: {self.turn_latencies}")
