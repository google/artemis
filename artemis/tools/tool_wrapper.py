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
import inspect
import time
from typing import Annotated, Any, get_args, get_origin
from unittest.mock import MagicMock, Mock
import uuid

from langchain_core.tools import BaseTool
from langchain_core.tools.base import InjectedToolCallId
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import BaseModel

from artemis.constants import VALIDATOR_MESSAGES_KEY
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

    if isinstance(tool, (Mock, MagicMock)):
        has_real_coro = hasattr(tool, "coroutine") and not isinstance(
            tool.coroutine, (Mock, MagicMock)
        )
        has_real_func = hasattr(tool, "func") and not isinstance(tool.func, (Mock, MagicMock))
        if not has_real_coro and not has_real_func:
            if (
                state is not None
                and hasattr(tool, "args")
                and isinstance(tool.args, dict)
                and "state" in tool.args
            ):
                final_args["state"] = state
            return await tool.ainvoke(final_args)

    # For StructuredTool, the original function might be in tool.func or tool.coroutine
    func = None
    if hasattr(tool, "coroutine") and tool.coroutine:
        func = tool.coroutine
    elif hasattr(tool, "func") and tool.func:
        func = tool.func

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
        except Exception:  # pylint: disable=broad-exception-caught
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
        # Fallback to ainvoke if no direct func available
        return await tool.ainvoke(final_args)


def get_tool_result_content(result: Any) -> Any:
    """Extracts the content from a tool result, handling Command objects."""
    if isinstance(result, Command):
        updates = result.update
        if VALIDATOR_MESSAGES_KEY in updates:
            msgs = updates[VALIDATOR_MESSAGES_KEY]
            if msgs and hasattr(msgs[0], "content"):
                return msgs[0].content
        return result
    return result
