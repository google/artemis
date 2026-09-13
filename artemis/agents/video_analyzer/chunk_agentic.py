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

"""Analyze video chunks through the Gemini Interactions API.

``processing: "agentic"`` lets the model search and revisit an uploaded clip.
Validation errors are returned as function results, with video context retained
through ``previous_interaction_id``. Only accepted answers reach the blackboard.

The stream consumer reconstructs function arguments from step deltas. Completion
events may omit steps, so it fetches the interaction when reconstruction fails.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
import json
from types import SimpleNamespace
from typing import Any

from artemis.agents.video_analyzer import gemini_files
from artemis.agents.video_analyzer import video_analyzer as _va
from artemis.agents.video_analyzer.chunk_conversation import (
    _NO_TOOL_CALL_ERROR,
    _SUBMIT_NOW_WARNING,
    _WRONG_TOOL_ERROR,
    SUB_AGENT_MAX_TURNS,
    SubAgentAnswerExhausted,
    _ChunkMedia,
    _clean_text,
    _persist_timeline_events,
    _tool_name,
    _validate_answer,
)
from artemis.agents.video_analyzer.universal_tools import UNIVERSAL_SUBMIT_ANSWER_TOOL
from artemis.data_engine.trace import CURRENT_TRACE_ID, TraceSpan
from artemis.llm.google import normalize_usage
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

STREAM_START_TIMEOUT_SECONDS = 60.0
INTERACTION_READ_TIMEOUT_SECONDS = 30.0

# Interaction statuses after which no follow-up turn can be sent
# (``google.genai`` ``InteractionStatus``).
_TERMINAL_FAILURE_STATUSES = frozenset({"failed", "cancelled", "incomplete", "budget_exceeded"})


def build_interactions_submit_tool() -> dict[str, Any]:
    """The ``submit_answer`` declaration in Interactions API (flat JSON schema) form.

    Derived from the universal (OpenAI-style) declaration so the schema is
    written once; ``timeline_events`` is required to match the native path.
    """
    function = UNIVERSAL_SUBMIT_ANSWER_TOOL["function"]
    parameters = copy.deepcopy(function["parameters"])
    required = list(parameters.get("required", []))
    if "timeline_events" not in required:
        required.append("timeline_events")
    parameters["required"] = required
    return {
        "type": "function",
        "name": function["name"],
        "description": function["description"],
        "parameters": parameters,
    }


def _steps(interaction: Any) -> list:
    return list(getattr(interaction, "steps", None) or [])


def _find_function_call(interaction: Any):
    """The last ``function_call`` step of an interaction, if any."""
    for step in reversed(_steps(interaction)):
        if getattr(step, "type", None) == "function_call":
            return step
    return None


def _raise_if_failed(interaction: Any) -> None:
    """Raises when an interaction ended in a terminal failure state.

    A follow-up with ``previous_interaction_id`` onto a failed interaction is
    rejected by the API, so the failure surfaces here as an exception and
    flows through the shared retry / classification layer instead.
    """
    status = str(getattr(interaction, "status", None) or "").lower()
    errors = list(getattr(interaction, "errors", None) or [])
    if status not in _TERMINAL_FAILURE_STATUSES and not errors:
        return
    details = "; ".join(
        f"[{getattr(error, 'code', None)}] {getattr(error, 'message', error)}" for error in errors
    )
    raise RuntimeError(
        f"Gemini Interactions interaction {getattr(interaction, 'id', None)} ended with"
        f" status {status or 'unknown'}" + (f": {details}" if details else "")
    )


async def run_agentic_chunk_conversation(
    analyzer,
    media: _ChunkMedia,
    current_start: float,
    current_end: float,
    start_time: float,
    end_time: float | None,
    specific_query: str,
    lease_owner: str,
) -> str:
    """Uploads the chunk and drives the agentic sub-agent to a committed answer."""
    file = None
    try:
        with TraceSpan(name="upload_video_to_gemini") as span:
            file = await analyzer.upload_and_poll_file(media.compressed_path)
            span.result = f"Uploaded {file.name}"
        logger.info(
            f"Invoking Gemini agentic video understanding for sub-agent task with model"
            f" {analyzer.model_name} (streaming)..."
        )
        trace_id = CURRENT_TRACE_ID.get()

        if analyzer.ctx.data_engine and trace_id:
            analyzer.ctx.data_engine.record_trace(
                type="llm_call",
                name="video_sub_agent",
                payload={
                    "contents": [
                        f"file://{file.name}",
                        media.prompt_with_context,
                    ],
                    "system_instruction": analyzer.sub_system_prompt,
                    "video_processing": "agentic",
                },
                parent_trace_id=trace_id,
            )

        final_summary, final_analysis = await _drive_agentic_loop(
            analyzer,
            file,
            trace_id,
            media,
            start_time,
            end_time,
            specific_query,
        )

        end_str = f"{current_end:.1f}s" if current_end is not None else "unknown"
        chunk_result = (
            f"[from {current_start:.1f}s to {end_str}] Summary:"
            f" {final_summary} Analysis: {final_analysis}"
        )
        analyzer.blackboard.complete_segment(
            current_start,
            current_end,
            specific_query,
            lease_owner,
            final_summary,
            final_analysis,
        )
        return chunk_result
    finally:
        if file:
            await gemini_files.delete_cloud_file(
                analyzer.client, file.name, analyzer.cloud_files_to_cleanup
            )


def _initial_request(analyzer, file, prompt: str, tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": analyzer.model_name,
        "input": [
            {
                "type": "video",
                "uri": file.uri,
                "mime_type": file.mime_type,
                "processing": "agentic",
            },
            {"type": "text", "text": prompt},
        ],
        "system_instruction": analyzer.sub_system_prompt,
        "tools": [tool],
        "stream": True,
    }


def _follow_up_request(
    analyzer, previous_id: str, steps: list[dict[str, Any]], tool: dict[str, Any]
) -> dict[str, Any]:
    """A stateful follow-up turn: the server keeps the video context.

    Only the conversation state is inherited through
    ``previous_interaction_id``; ``system_instruction`` and ``tools`` are
    interaction-scoped and must be re-sent on every turn.
    """
    return {
        "model": analyzer.model_name,
        "previous_interaction_id": previous_id,
        "input": steps,
        "system_instruction": analyzer.sub_system_prompt,
        "tools": [tool],
        "stream": True,
    }


def _function_result(call_id: str, error: str) -> dict[str, Any]:
    return {
        "type": "function_result",
        "call_id": call_id,
        "name": "submit_answer",
        "result": {"error": error},
    }


async def _drive_agentic_loop(
    analyzer,
    file,
    trace_id,
    media: _ChunkMedia,
    start_time: float,
    end_time: float | None,
    specific_query: str,
) -> tuple[str, str]:
    """Return a validated answer and persist its timeline events.

    Raise ``SubAgentAnswerExhausted`` if no answer is accepted within the turn limit.
    """
    tool = build_interactions_submit_tool()
    request = _initial_request(analyzer, file, media.prompt_with_context, tool)
    rejection = _NO_TOOL_CALL_ERROR

    for turn in range(1, SUB_AGENT_MAX_TURNS + 1):
        interaction = await _stream_agentic_turn(analyzer, request, trace_id)
        interaction_id = getattr(interaction, "id", None) or ""
        call = _find_function_call(interaction)

        if call is None:
            rejection = _NO_TOOL_CALL_ERROR
            steps = [{"type": "text", "text": _SUBMIT_NOW_WARNING}]
        elif _tool_name(getattr(call, "name", None)) != "submit_answer":
            rejection = _WRONG_TOOL_ERROR
            steps = [_function_result(call.id, rejection)]
        else:
            args = dict(getattr(call, "arguments", None) or {})
            timeline_events = args.get("timeline_events", [])
            if not isinstance(timeline_events, list):
                timeline_events = []
            summary = _clean_text(args.get("summary"))
            analysis = _clean_text(args.get("analysis"))

            rejection = _validate_answer(summary, timeline_events, start_time, end_time)
            if rejection is None:
                await _persist_timeline_events(
                    analyzer, timeline_events, media, start_time, end_time, specific_query
                )
                return summary, analysis or "No analysis provided."
            steps = [_function_result(call.id, rejection)]

        if turn < SUB_AGENT_MAX_TURNS:
            request = _follow_up_request(analyzer, interaction_id, steps, tool)

    raise SubAgentAnswerExhausted(SUB_AGENT_MAX_TURNS, rejection)


async def _stream_agentic_turn(analyzer, request: dict[str, Any], trace_id):
    """Runs one traced streaming Interactions turn and returns the completed interaction."""
    with TraceSpan(name="gemini_agentic_video_call") as span:
        span.payload = {
            "model": request.get("model"),
            "video_processing": "agentic",
            "previous_interaction_id": request.get("previous_interaction_id"),
        }
        stream = await asyncio.wait_for(
            analyzer.client.aio.interactions.create(**request),
            timeout=STREAM_START_TIMEOUT_SECONDS,
        )
        interaction = await asyncio.wait_for(
            _consume_agentic_stream(analyzer, span, stream, trace_id),
            timeout=analyzer.agentic_call_timeout_seconds,
        )
        call = _find_function_call(interaction)
        span.result = (
            f"Function call: {_tool_name(getattr(call, 'name', None))}"
            if call is not None
            else f"Status: {getattr(interaction, 'status', 'unknown')}"
        )
    return interaction


@dataclass
class _PendingStep:
    """A step under construction while its start/delta/stop events stream in."""

    kind: str
    call_id: str | None = None
    name: str | None = None
    seed_arguments: dict[str, Any] = field(default_factory=dict)
    arguments_json: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)

    def finish(self):
        """Materializes the step in the shape the SDK's typed steps expose.

        Returns ``None`` for a function call whose streamed arguments are not
        valid JSON: a half-built call must not stand in for the real one, so
        the step is dropped and the interaction is re-read by id instead.
        """
        if self.kind == "function_call":
            arguments: dict[str, Any] = dict(self.seed_arguments)
            raw = "".join(self.arguments_json)
            if raw.strip():
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    logger.warning(
                        f"Could not parse streamed arguments for {self.name}: {raw[:200]!r};"
                        " dropping the step"
                    )
                    return None
                if isinstance(parsed, dict):
                    arguments = parsed
            return SimpleNamespace(
                type="function_call", id=self.call_id, name=self.name, arguments=arguments
            )
        if self.kind == "model_output":
            return SimpleNamespace(
                type="model_output",
                content=[SimpleNamespace(type="text", text="".join(self.text))],
            )
        return SimpleNamespace(type=self.kind, id=self.call_id)


async def _consume_agentic_stream(analyzer, span, stream, trace_id):
    """Consumes Interactions SSE events, rebuilding steps and mirroring progress.

    Returns an interaction-shaped object (``id`` / ``status`` / ``steps`` /
    ``usage``). The interaction is re-read by id when the stream ended
    without a completion event, or when it stopped at ``requires_action``
    without a parsable function call.
    """
    data_engine = analyzer.ctx.data_engine if trace_id else None
    interaction_id: str | None = None
    status: str | None = None
    completed_steps: list | None = None
    usage: dict[str, int] | None = None
    pending: dict[int, _PendingStep] = {}
    steps: list = []
    processing_calls = 0

    def _emit(text: str, *, thinking: bool) -> None:
        if data_engine and text:
            data_engine.stream_output(trace_id, text, is_thinking=thinking)

    async for event in stream:
        data = getattr(event, "data", event)
        event_type = getattr(data, "event_type", None)

        if event_type == "interaction.created":
            created = getattr(data, "interaction", None)
            interaction_id = getattr(created, "id", None) or interaction_id
        elif event_type == "step.start":
            step = getattr(data, "step", None)
            index = getattr(data, "index", None)
            kind = str(getattr(step, "type", None) or "")
            pending[index] = _PendingStep(
                kind=kind,
                call_id=getattr(step, "id", None) or getattr(step, "call_id", None),
                name=getattr(step, "name", None),
                seed_arguments=dict(getattr(step, "arguments", None) or {}),
            )
            if kind == "processing_call":
                processing_calls += 1
                _emit(
                    f"Inspecting the recording (video lookup {processing_calls})\n",
                    thinking=True,
                )
        elif event_type == "step.delta":
            delta = getattr(data, "delta", None)
            delta_type = getattr(delta, "type", None)
            current = pending.get(getattr(data, "index", None))
            if delta_type == "arguments_delta":
                fragment = getattr(delta, "arguments", None)
                if current is not None and isinstance(fragment, str):
                    current.arguments_json.append(fragment)
            elif delta_type == "text":
                text = getattr(delta, "text", None)
                if current is not None and isinstance(text, str):
                    current.text.append(text)
                _emit(text or "", thinking=False)
            elif delta_type == "thought_summary":
                content = getattr(delta, "content", None)
                _emit(getattr(content, "text", None) or "", thinking=True)
        elif event_type == "step.stop":
            current = pending.pop(getattr(data, "index", None), None)
            finished = current.finish() if current is not None else None
            if finished is not None:
                steps.append(finished)
            usage = normalize_usage(getattr(data, "usage", None)) or usage
        elif event_type == "interaction.completed":
            completed = getattr(data, "interaction", None)
            interaction_id = getattr(completed, "id", None) or interaction_id
            status = getattr(completed, "status", None) or status
            completed_steps = getattr(completed, "steps", None) or None
            usage = normalize_usage(getattr(completed, "usage", None)) or usage
            _raise_if_failed(completed)
        elif event_type == "interaction.status_update":
            status = getattr(data, "status", None) or status
        elif event_type == "error":
            error = getattr(data, "error", None)
            raise RuntimeError(
                "Gemini Interactions stream error"
                f" [{getattr(error, 'code', None)}]: {getattr(error, 'message', None)}"
            )

    for current in pending.values():
        finished = current.finish()
        if finished is not None:
            steps.append(finished)
    span.payload["processing_calls"] = processing_calls
    if completed_steps:
        steps = list(completed_steps)

    needs_reread = status is None or (
        status == "requires_action" and _find_function_call(SimpleNamespace(steps=steps)) is None
    )
    if needs_reread:
        if not interaction_id:
            raise RuntimeError("Gemini Interactions stream ended without an interaction")
        logger.info(f"Re-reading interaction {interaction_id} (status={status})")
        fetched = await asyncio.wait_for(
            analyzer.client.aio.interactions.get(interaction_id),
            timeout=INTERACTION_READ_TIMEOUT_SECONDS,
        )
        _raise_if_failed(fetched)
        status = getattr(fetched, "status", None) or status
        steps = _steps(fetched) or steps
        usage = normalize_usage(getattr(fetched, "usage", None)) or usage

    if usage:
        span.payload["usage_metadata"] = usage
    return SimpleNamespace(id=interaction_id, status=status, steps=steps, usage=usage)
