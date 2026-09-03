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

"""Native Gemini coordinator loop for the video analyzer main agent.

Extracted from ``video_analyzer.py`` as a pure structural split.  Patchable
collaborators are looked up late through the facade module so
``mock.patch("artemis.agents.video_analyzer.video_analyzer.<name>")`` targets
keep working.
"""

import asyncio
import os
from pathlib import Path
import shutil
import tempfile

from google.genai import types

from artemis.agents.video_analyzer import video_analyzer as _va
from artemis.agents.video_analyzer.reliability import classify_video_failure
from artemis.constants import SAFETY_SETTINGS_BLOCK_NONE
from artemis.data_engine.trace import CURRENT_TRACE_ID, TraceSpan
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class _DummyResponse:
    """Aggregated view over a consumed stream, shaped like a full response."""

    def __init__(self, text, function_calls, parts):
        self.text = text
        self.function_calls = function_calls
        self.candidates = [
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=parts if parts else ([types.Part.from_text(text=text)] if text else []),
                )
            )
        ]


async def run_native(
    analyzer,
    ctx,
    time_description: str,
    purpose: str,
    system_prompt: str,
    temperature: float,
    thinking_level,
) -> tuple[str, str]:
    """Drives the native Gemini coordinator loop to a final answer."""
    tools_declaration = analyzer.tools_declaration

    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(
                    text=(f"Time description: {time_description}\nPurpose: {purpose}")
                )
            ],
        )
    ]

    agent_outcome = ""
    status = "success"
    max_iterations = 10
    iterations = 0
    last_ledger_index = 0

    try:
        while iterations < max_iterations:
            iterations += 1
            logger.info(f"Iteration {iterations}: Invoking Native Gemini SDK...")

            analyzer._clean_blackboard()

            last_ledger_index = _append_ledger_diff(analyzer, contents, last_ledger_index)

            is_final_turn = iterations == max_iterations
            _append_turn_guidance(contents, iterations, max_iterations, is_final_turn)

            response = await _stream_main_turn(
                analyzer,
                ctx,
                contents,
                system_prompt,
                temperature,
                thinking_level,
                tools_declaration,
                is_final_turn,
            )

            function_calls = response.function_calls
            if not function_calls:
                agent_outcome = response.text
                break

            if response.candidates and response.candidates[0].content:
                contents.append(response.candidates[0].content)
            else:
                contents.append(
                    types.Content(
                        role="model",
                        parts=[types.Part(function_call=fc) for fc in function_calls],
                    )
                )

            tool_response_parts = await _execute_native_tool_calls(analyzer, function_calls)

            if tool_response_parts:
                contents.append(types.Content(role="user", parts=tool_response_parts))

        if iterations >= max_iterations and not agent_outcome:
            agent_outcome = (
                "Error: Video analyzer agent reached maximum iterations"
                " without settling on a final answer."
            )
            status = "error"

    except Exception as e:
        logger.error(f"Video analyzer failed: {e}")
        failure = classify_video_failure(e)
        if failure.should_fallback:
            logger.warning(
                f"Native coordinator failed with {failure.category.value}; "
                "continuing through universal fallback"
            )
            agent_outcome, status = await analyzer._run_universal(
                time_description,
                purpose,
                system_prompt,
                force_fallback=True,
            )
        else:
            agent_outcome = f"Video analysis failed: {e}"
            status = "error"
    finally:
        analyzer._record_reliability_metrics()
        await _cleanup_native_run(analyzer)

    return agent_outcome, status


def _append_ledger_diff(analyzer, contents: list, last_ledger_index: int) -> int:
    """Appends new blackboard entries (and proofs) since the last coordinator turn."""
    new_entries = analyzer.blackboard_entries[last_ledger_index:]
    if analyzer.enable_ledger and new_entries:
        lines = []
        image_parts = []
        for e in new_entries:
            line = f"[{e['start']}s - {e['end']}s] SUMMARY: {e['summary']}"
            screenshot_file = e.get("screenshot") or e.get("screenshot_path")
            if screenshot_file and Path(screenshot_file).exists():
                line += f" PROOF: {Path(screenshot_file).name}"
                image_parts.append(
                    types.Part.from_text(text=f"PROOF: {Path(screenshot_file).name}")
                )
                image_parts.append(
                    types.Part.from_bytes(
                        data=Path(screenshot_file).read_bytes(),
                        mime_type="image/jpeg",
                    )
                )
            lines.append(line)
        diff_text = "NEW BLACKBOARD ENTRIES ADDED: " + " | ".join(lines)
        contents.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=diff_text)],
            )
        )
        if image_parts:
            contents.append(types.Content(role="user", parts=image_parts))
        last_ledger_index = len(analyzer.blackboard_entries)
    return last_ledger_index


def _append_turn_guidance(
    contents: list, iterations: int, max_iterations: int, is_final_turn: bool
) -> None:
    """Appends per-turn steering messages (final-turn notice or progress nudge)."""
    if is_final_turn:
        contents.append(
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text=(
                            "This is your final iteration; all"
                            " tools are stripped, and you must"
                            " provide your final answer directly."
                        )
                    )
                ],
            )
        )
    else:
        if iterations > 1:
            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(
                            text=(
                                "You have not completed the video"
                                " analysis yet (iteration"
                                f" {iterations} of"
                                f" {max_iterations})."
                            )
                        )
                    ],
                )
            )


def _record_main_llm_trace(
    ctx, trace_id, contents: list, system_prompt: str, tools_declaration, is_final_turn: bool
) -> None:
    """Records the sanitized coordinator LLM call into the trace store."""
    serialized_contents = []
    for c in contents:
        if hasattr(c, "model_dump"):
            dumped = c.model_dump()
            if isinstance(dumped, dict) and "parts" in dumped and dumped["parts"]:
                for p in dumped["parts"]:
                    if (
                        isinstance(p, dict)
                        and "inline_data" in p
                        and p["inline_data"]
                        and "data" in p["inline_data"]
                    ):
                        p["inline_data"]["data"] = "<image_bytes_sanitized>"
            serialized_contents.append(dumped)
        else:
            serialized_contents.append(str(c))

    ctx.data_engine.record_trace(
        type="llm_call",
        name="video_analyzer_main",
        payload={
            "contents": serialized_contents,
            "system_instruction": system_prompt,
            "tools": (
                [t.name for t in tools_declaration]
                if (tools_declaration and not is_final_turn)
                else []
            ),
        },
        parent_trace_id=trace_id,
    )


async def _stream_main_turn(
    analyzer,
    ctx,
    contents: list,
    system_prompt: str,
    temperature: float,
    thinking_level,
    tools_declaration,
    is_final_turn: bool,
):
    """Runs one traced, retry-wrapped streaming turn of the main coordinator."""
    with TraceSpan(name="gemini_main_agent_call") as span:
        trace_id = CURRENT_TRACE_ID.get()

        if ctx.data_engine and trace_id:
            _record_main_llm_trace(
                ctx, trace_id, contents, system_prompt, tools_declaration, is_final_turn
            )

        async def run_stream():
            return await _consume_main_stream(
                analyzer,
                ctx,
                span,
                trace_id,
                contents,
                system_prompt,
                temperature,
                thinking_level,
                tools_declaration,
                is_final_turn,
            )

        response = await _va._invoke_with_retry(
            lambda: asyncio.wait_for(run_stream(), timeout=300),
            "Native Gemini video coordinator",
        )
        span.result = (
            f"Function calls: {len(response.function_calls)}"
            if response.function_calls
            else "Final answer"
        )
    return response


async def _consume_main_stream(
    analyzer,
    ctx,
    span,
    trace_id,
    contents: list,
    system_prompt: str,
    temperature: float,
    thinking_level,
    tools_declaration,
    is_final_turn: bool,
) -> _DummyResponse:
    """Starts the coordinator stream and aggregates it into a response object."""
    stream = await asyncio.wait_for(
        analyzer.client.aio.models.generate_content_stream(
            model=analyzer.model_name,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=temperature,
                tools=[]
                if is_final_turn
                else [types.Tool(function_declarations=tools_declaration)],
                safety_settings=SAFETY_SETTINGS_BLOCK_NONE,
                **(
                    {"thinking_config": types.ThinkingConfig(thinking_level=thinking_level)}
                    if thinking_level
                    else {}
                ),
            ),
        ),
        timeout=60,
    )

    text = ""
    accumulated_parts = []
    function_calls = []

    async for chunk in stream:
        if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
            span.payload["usage_metadata"] = {
                "prompt_token_count": getattr(
                    chunk.usage_metadata,
                    "prompt_token_count",
                    0,
                ),
                "candidates_token_count": getattr(
                    chunk.usage_metadata,
                    "candidates_token_count",
                    0,
                ),
                "total_token_count": getattr(
                    chunk.usage_metadata,
                    "total_token_count",
                    0,
                ),
            }

        chunk_text = chunk.text or ""
        text += chunk_text
        if ctx.data_engine and trace_id and chunk_text:
            ctx.data_engine.stream_output(trace_id, chunk_text, is_thinking=False)

        if chunk.function_calls:
            function_calls.extend(chunk.function_calls)

        candidates = getattr(chunk, "candidates", None)
        if candidates and isinstance(candidates, list) and len(candidates) > 0:
            content = getattr(candidates[0], "content", None)
            parts = getattr(content, "parts", None) if content else None
            if parts:
                for part in parts:
                    accumulated_parts.append(part)
                    if getattr(part, "thought", False) and part.text:
                        if ctx.data_engine and trace_id:
                            ctx.data_engine.stream_output(
                                trace_id,
                                part.text,
                                is_thinking=True,
                            )
        else:
            if chunk.function_calls:
                for fc in chunk.function_calls:
                    accumulated_parts.append(types.Part(function_call=fc))

    return _DummyResponse(text, function_calls, accumulated_parts)


async def _execute_native_tool_calls(analyzer, function_calls: list) -> list:
    """Executes coordinator tool calls (parallel where safe) and collects responses."""
    sub_agent_calls = [
        fc
        for fc in function_calls
        if (fc.name.split(":")[-1] if ":" in fc.name else fc.name) == "spawn_sub_agent"
    ]
    audio_only_calls = [
        fc
        for fc in function_calls
        if (fc.name.split(":")[-1] if ":" in fc.name else fc.name) == "analyze_audio_only"
    ]
    other_calls = [
        fc
        for fc in function_calls
        if (fc.name.split(":")[-1] if ":" in fc.name else fc.name)
        not in ["spawn_sub_agent", "analyze_audio_only"]
    ]

    tool_response_parts = []

    if sub_agent_calls:
        await _run_spawn_calls(analyzer, sub_agent_calls, tool_response_parts)

    if audio_only_calls:
        await _run_audio_calls(analyzer, audio_only_calls, tool_response_parts)

    await _run_other_calls(analyzer, other_calls, tool_response_parts)

    return tool_response_parts


async def _run_spawn_calls(analyzer, sub_agent_calls: list, tool_response_parts: list) -> None:
    """Runs spawn_sub_agent calls in parallel and appends their responses."""
    logger.info(f"Executing {len(sub_agent_calls)} sub-agent calls in parallel...")

    async def bounded_spawn(fc):
        # exec_spawn_sub_agent limits each actual model leaf.
        # Holding the same semaphore around the coordinator
        # would deadlock when all slots wait for child slots.
        return await analyzer.exec_spawn_sub_agent(
            fc.args.get("start_time"),
            fc.args.get("end_time"),
            fc.args.get("specific_query"),
        )

    tasks = [bounded_spawn(fc) for fc in sub_agent_calls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for fc, result in zip(sub_agent_calls, results):
        if isinstance(result, Exception):
            error_msg = f"Sub-agent failed. Error: {result}"
            logger.error(error_msg)
            tool_response_parts.append(
                types.Part.from_function_response(name=fc.name, response={"error": error_msg})
            )
        else:
            tool_response_parts.append(
                types.Part.from_function_response(name=fc.name, response={"result": result})
            )


async def _run_audio_calls(analyzer, audio_only_calls: list, tool_response_parts: list) -> None:
    """Runs analyze_audio_only calls in parallel and appends their responses."""
    logger.info(f"Executing {len(audio_only_calls)} analyze_audio_only calls in parallel...")

    async def bounded_audio_spawn(fc):
        async with _va.API_SEMAPHORE:
            return await analyzer.exec_analyze_audio_only(
                fc.args.get("start_time"),
                fc.args.get("end_time"),
                fc.args.get("specific_query"),
            )

    tasks = [bounded_audio_spawn(fc) for fc in audio_only_calls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for fc, result in zip(audio_only_calls, results):
        if isinstance(result, Exception):
            error_msg = f"Audio analysis failed. Error: {result}"
            logger.error(error_msg)
            tool_response_parts.append(
                types.Part.from_function_response(name=fc.name, response={"error": error_msg})
            )
        else:
            tool_response_parts.append(
                types.Part.from_function_response(name=fc.name, response={"result": result})
            )


async def _run_other_calls(analyzer, other_calls: list, tool_response_parts: list) -> None:
    """Runs remaining coordinator tool calls sequentially."""
    for fc in other_calls:
        name = fc.name.split(":")[-1] if ":" in fc.name else fc.name
        args = fc.args
        logger.info(f"Executing tool '{name}' sequentially...")

        if name == "extract_segment_metadata":
            try:
                res = await analyzer.exec_extract_segment_metadata(
                    args.get("start_time"), args.get("end_time")
                )
                tool_response_parts.append(
                    types.Part.from_function_response(name=name, response={"result": res})
                )
            except Exception as e:
                tool_response_parts.append(
                    types.Part.from_function_response(name=name, response={"error": str(e)})
                )


async def _cleanup_native_run(analyzer) -> None:
    """Releases remaining cloud files and temporary local media after a run."""
    if analyzer.cloud_files_to_cleanup:
        logger.info(
            f"Cleaning up {len(analyzer.cloud_files_to_cleanup)} remaining"
            " cloud files in parallel..."
        )
        tasks = [
            asyncio.wait_for(analyzer.client.aio.files.delete(name=name), timeout=30)
            for name in list(analyzer.cloud_files_to_cleanup)
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        analyzer.cloud_files_to_cleanup.clear()

    if os.environ.get("KEEP_VIDEOS") or os.environ.get("ARTEMIS_DEBUG"):
        logger.info("Skipping cleanup of local video files and directories due to debug mode.")
    else:
        for path in list(analyzer.local_files_to_cleanup):
            try:
                if path.exists():
                    logger.info(f"Cleaning up local file {path}...")
                    path.unlink()
            except Exception as e:
                logger.error(f"Failed to delete local file {path}: {e}")

        for dir_path in list(analyzer.local_dirs_to_cleanup):
            try:
                resolved = dir_path.resolve()
                system_dirs = {
                    Path("/").resolve(),
                    Path("/tmp").resolve(),
                    Path("/var/tmp").resolve(),
                    Path.home().resolve(),
                    Path(tempfile.gettempdir()).resolve(),
                }
                if resolved in system_dirs:
                    logger.debug(f"Skipping deletion of root/system directory: {dir_path}")
                    continue

                if dir_path.exists():
                    logger.info(f"Cleaning up local directory {dir_path}...")
                    shutil.rmtree(dir_path, ignore_errors=True)
            except Exception as e:
                logger.error(f"Failed to delete local directory {dir_path}: {e}")
