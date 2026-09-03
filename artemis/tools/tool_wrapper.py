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

from collections.abc import Callable
import functools
import inspect
import re
import time
from typing import Annotated, Any, Literal, get_args, get_origin
import uuid

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.tools.base import InjectedToolCallId
from langgraph.prebuilt import InjectedState
from pydantic import BaseModel

from artemis.context import ArtemisContext
from artemis.data_engine import engine as engine_mod
from artemis.data_engine.trace import CURRENT_TRACE_ID, smart_serialize
from artemis.graph.state import State
from artemis.tools.types import CyFunctionDetector


class ToolWrapper(BaseModel):
    """Wrapper holding a tool factory and lifecycle callbacks."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    tool_fn_getter: Callable[[ArtemisContext], BaseTool]
    on_success_fn: Callable[..., str]
    on_failure_fn: Callable[..., str]
    is_available_fn: Callable[[ArtemisContext], bool] | None = None


class CompositeToolWrapper(ToolWrapper):
    """Wrapper holding a composite tool factory and lifecycle callbacks."""

    composite_tools_fn_getter: Callable[[ArtemisContext], list[BaseTool]]


# pylint: disable=too-many-branches,too-many-locals,too-many-statements
async def invoke_tool_with_injection(
    tool: BaseTool,
    args: dict,
    tool_call_id: str,
    state: State | None = None,
    record_trace: bool = True,
) -> Any:
    """Invokes a LangChain tool, manually injecting arguments marked with

    InjectedState or InjectedToolCallId if calling the underlying function
    directly.
    Automatically records type='tool' trace in DataEngine.
    """
    final_args = dict(args)

    def _real_callable(candidate: Any) -> Callable | None:
        """Return a routine or partial whose signature supports direct injection."""
        target = candidate.func if isinstance(candidate, functools.partial) else candidate
        if inspect.isroutine(target):
            return candidate
        return None

    # For StructuredTool, the original function might be in tool.func or tool.coroutine
    func = None
    if getattr(tool, "coroutine", None):
        func = _real_callable(tool.coroutine)
    if func is None and getattr(tool, "func", None):
        func = _real_callable(tool.func)

    if func:
        try:
            sig = inspect.signature(func)
            for param_name, param in sig.parameters.items():
                annotation = param.annotation
                # Handle Annotated types
                if get_origin(annotation) is Annotated:
                    args_list = get_args(annotation)
                    if InjectedState in args_list:
                        final_args[param_name] = state
                    elif InjectedToolCallId in args_list:
                        final_args[param_name] = tool_call_id
        except (ValueError, TypeError):
            # If inspection fails, fall back to just using provided args
            pass

        trace_id = uuid.uuid4()
        parent_id = CURRENT_TRACE_ID.get()
        token = CURRENT_TRACE_ID.set(trace_id)
        start_time = time.time()

        live_engine = getattr(engine_mod, "_CURRENT_DATA_ENGINE", None)

        if record_trace and live_engine:
            step_id = getattr(live_engine, "current_step_id", None)
            live_engine.record_trace(
                type="tool",
                name=tool.name,
                payload={"args": {k: smart_serialize(v) for k, v in args.items()}},
                status="running",
                parent_trace_id=parent_id,
                step_id=step_id,
                trace_id=trace_id,
            )

        execution_status = "running"
        execution_result = None
        execution_error = None
        try:
            # Call the function
            if inspect.iscoroutinefunction(func):
                result = await func(**final_args)
            else:
                result = func(**final_args)
            execution_status = "success"
            execution_result = result
            return result
        except Exception as e:
            execution_status = "failed"
            execution_error = e
            raise e
        finally:
            duration = time.time() - start_time
            if record_trace and live_engine:
                step_id = getattr(live_engine, "current_step_id", None)
                payload = {"args": {k: smart_serialize(v) for k, v in args.items()}}

                if execution_status == "success":
                    payload["result"] = smart_serialize(get_tool_result_content(execution_result))
                elif execution_status == "failed":
                    payload["error"] = str(execution_error)
                else:
                    execution_status = "terminated"
                    payload["error"] = "Execution terminated unexpectedly."

                live_engine.record_trace(
                    type="tool",
                    name=tool.name,
                    payload=payload,
                    status=execution_status,
                    duration=duration,
                    parent_trace_id=parent_id,
                    step_id=step_id,
                    trace_id=trace_id,
                )
            CURRENT_TRACE_ID.reset(token)
    else:
        # Fallback to ainvoke if no direct func available; tools that declare
        # a "state" argument still receive the graph state.
        tool_args = getattr(tool, "args", None)
        if state is not None and isinstance(tool_args, dict) and "state" in tool_args:
            final_args["state"] = state
        return await tool.ainvoke(final_args)


def get_tool_result_content(result: Any) -> Any:
    """Extracts the content from a tool result, handling ToolMessage returns."""
    if isinstance(result, ToolMessage):
        return result.content
    return result


# --- Multimodal tool results ------------------------------------------------------------

ImageCarrier = Literal["tool", "human"]

#: Providers whose tool-result messages may carry image parts directly.
_TOOL_IMAGE_PROVIDERS = frozenset({"google", "anthropic"})

_STEP_RE = re.compile(r"\bstep\s+(\d+)", re.IGNORECASE)


def split_multimodal_result(result: Any) -> tuple[str, list[dict[str, Any]]]:
    """Splits a tool result into its text and its image content blocks.

    Plain strings have no images; content-block lists contribute their text
    blocks (joined by newlines) and their ``image_url`` / ``image`` blocks.
    ``ToolMessage`` results are unwrapped first.
    """
    content = get_tool_result_content(result)
    if content is None:
        return "", []
    if isinstance(content, list):
        texts: list[str] = []
        images: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("image_url", "image"):
                images.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text") or ""))
            else:
                texts.append(str(block))
        return "\n".join(t for t in texts if t), images
    return content if isinstance(content, str) else str(content), []


def resolve_image_carrier(llm_or_provider: Any) -> ImageCarrier:
    """Which message carries images returned by a tool for this LLM.

    Gemini (direct API) and Anthropic accept image parts inside the tool
    result message itself (``"tool"``). Vertex AI and the OpenAI-compatible
    providers (openai / openrouter / xai / ...) reject them there, so the
    image travels in a ``HumanMessage`` that immediately follows the textual
    tool result (``"human"``). Accepts a provider name, a provider enum, or an
    LLM whose ``endpoint.provider`` names the provider that actually issues
    the request (a fallback model is judged by its own provider).
    """
    provider: Any = llm_or_provider
    if provider is not None and not isinstance(provider, str):
        # An LLM (RobustChatModelWrapper) names its provider on ``endpoint``;
        # anything without an endpoint is taken as the provider itself.
        endpoint = getattr(provider, "endpoint", None)
        if endpoint is not None:
            provider = getattr(endpoint, "provider", None)
    value = str(getattr(provider, "value", provider) or "").lower()
    return "tool" if value in _TOOL_IMAGE_PROVIDERS else "human"


def tool_result_messages(
    tool_call_id: str,
    result: Any,
    *,
    name: str | None = None,
    status: str = "success",
    image_carrier: ImageCarrier | None = None,
    llm: Any = None,
) -> list[BaseMessage]:
    """The conversation messages that deliver one tool result to the model.

    Text-only results become a single ``ToolMessage``. Results carrying images
    (e.g. ``get_step_screenshot``) are delivered according to the image
    carrier — given explicitly or resolved from ``llm`` — so every agent loop
    handles multimodal tool results the same way and no loop ever pastes
    base64 into a text field. The tool call id, name and status are preserved.
    """
    text, images = split_multimodal_result(result)
    extra = {"name": name} if name else {}
    if not images:
        content = get_tool_result_content(result)
        if not isinstance(content, (str, list)):
            content = text
        return [ToolMessage(tool_call_id=tool_call_id, content=content, status=status, **extra)]

    carrier = image_carrier or resolve_image_carrier(llm)
    if carrier == "tool":
        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
        return [
            ToolMessage(
                tool_call_id=tool_call_id, content=[*blocks, *images], status=status, **extra
            )
        ]

    match = _STEP_RE.search(text)
    caption = f"[Screenshot returned by {name or 'the tool'}"
    if match:
        caption += f" for step {match.group(1)}"
    caption += "]"
    return [
        ToolMessage(tool_call_id=tool_call_id, content=text, status=status, **extra),
        HumanMessage(content=[{"type": "text", "text": caption}, *images]),
    ]
