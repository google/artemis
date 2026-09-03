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
from collections.abc import Callable
import functools
import hashlib
import inspect
import json
import time
from typing import Any
import uuid
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool

from artemis.context import ArtemisContext
from artemis.data_engine.context_vars import (
    CURRENT_NODE_NAME,
    CURRENT_TRACE_ID,
)


def smart_serialize(obj: Any) -> str:
    """Smartly serializes an object to string, handling complex types."""

    if obj.__class__.__name__ == "State":
        return "<State>"

    if isinstance(obj, (dict, list)):
        try:
            cleaned = _deep_smart_clean(obj)
            return json.dumps(cleaned, ensure_ascii=False)
        except Exception:
            return str(obj)
    elif isinstance(obj, bytes):
        return f"<Bytes length={len(obj)} sha256={hashlib.sha256(obj).hexdigest()[:8]}>"
    elif isinstance(obj, str):
        if len(obj) > 5000:
            clean_str = obj
            if "base64," in clean_str:
                clean_str = clean_str.split("base64,")[-1]

            try:
                img_bytes = base64.b64decode(clean_str, validate=False)
                if len(img_bytes) > 1000:
                    sha = hashlib.sha256(img_bytes).hexdigest()
                    return f"<ImageRef: sha256={sha} length={len(obj)}>"
            except (ValueError, TypeError):
                pass
            return (
                f"<Massive String length={len(obj)} characters (truncated for"
                " Data Engine performance)>"
            )
        return obj
    else:
        return str(obj)


def _deep_smart_clean(obj: Any) -> Any:
    """Recursively cleans dictionaries and lists using smart_serialize rules for strings and bytes."""
    if isinstance(obj, dict):
        return {k: _deep_smart_clean(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_deep_smart_clean(v) for v in obj]
    elif isinstance(obj, bytes):
        return f"<Bytes length={len(obj)} sha256={hashlib.sha256(obj).hexdigest()[:8]}>"
    elif isinstance(obj, str):
        if len(obj) > 5000:
            clean_str = obj
            if "base64," in clean_str:
                clean_str = clean_str.split("base64,")[-1]

            try:
                img_bytes = base64.b64decode(clean_str, validate=False)
                if len(img_bytes) > 1000:
                    sha = hashlib.sha256(img_bytes).hexdigest()
                    return f"<ImageRef: sha256={sha} length={len(obj)}>"
            except (ValueError, TypeError):
                pass
            return (
                f"<Massive String length={len(obj)} characters (truncated for"
                " Data Engine performance)>"
            )
        return obj
    else:
        return obj


def serialize_error(error: Any) -> str:
    """Returns a user-friendly and informative string representation of an exception or error message."""
    if not error:
        return ""
    if isinstance(error, str):
        return error
    err_str = str(error)
    if not err_str:
        return f"{error.__class__.__name__}"
    return f"{error.__class__.__name__}: {err_str}"


class DataEngineCallbackHandler(BaseCallbackHandler):
    """Callback handler to record executions to DataEngine."""

    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx
        self.running_spans = {}
        self.chain_parents = {}

    def _resolve_parent_id(
        self, parent_run_id: uuid.UUID | None, kwargs: dict[str, Any] = None
    ) -> uuid.UUID | None:
        curr = parent_run_id
        while curr in self.chain_parents:
            curr = self.chain_parents[curr]
        if curr:
            return curr

        # Check in metadata passed via kwargs
        if kwargs and "metadata" in kwargs:
            metadata = kwargs["metadata"] or {}
            p_id = metadata.get("parent_trace_id")
            if p_id:
                try:
                    return uuid.UUID(p_id)
                except ValueError:
                    pass

        return CURRENT_TRACE_ID.get()

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        self.chain_parents[run_id] = parent_run_id or CURRENT_TRACE_ID.get()

    def on_chain_end(self, outputs: dict[str, Any], *, run_id: uuid.UUID, **kwargs: Any) -> Any:
        self.chain_parents.pop(run_id, None)

    def on_chain_error(self, error: Any, *, run_id: uuid.UUID, **kwargs: Any) -> Any:
        self.chain_parents.pop(run_id, None)

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        pass

    def on_tool_end(self, output: str, *, run_id: uuid.UUID, **kwargs: Any) -> Any:
        pass

    def on_tool_error(self, error: Any, *, run_id: uuid.UUID, **kwargs: Any) -> Any:
        pass

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run when LLM starts running (completion API)."""
        if self.ctx.data_engine:
            name = serialized.get("name") or "llm"
            self.running_spans[run_id] = {
                "prompts": prompts,
                "name": name,
                "start_time": time.time(),
            }

            self.ctx.data_engine.record_trace(
                type="llm_call",
                name=name,
                payload={"prompts": prompts},
                trace_id=run_id,
                parent_trace_id=self._resolve_parent_id(parent_run_id, kwargs),
                status="running",
                step_id=self.ctx.data_engine.current_step_id,
            )

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run when Chat Model starts running."""
        if self.ctx.data_engine:
            flat_messages = []
            if messages:
                for msg in messages[0]:
                    flat_messages.append(self._serialize_message(msg))

            name = serialized.get("name") or "llm"
            self.running_spans[run_id] = {
                "messages": flat_messages,
                "name": name,
                "start_time": time.time(),
            }

            self.ctx.data_engine.record_trace(
                type="llm_call",
                name=name,
                payload={"messages": flat_messages},
                trace_id=run_id,
                parent_trace_id=self._resolve_parent_id(parent_run_id, kwargs),
                status="running",
                step_id=self.ctx.data_engine.current_step_id,
            )

    def _safe_uuid5(self, parent_id, name: str) -> uuid.UUID:
        try:
            pid = parent_id if isinstance(parent_id, uuid.UUID) else uuid.UUID(str(parent_id))
            return uuid.uuid5(pid, name)
        except Exception:
            return uuid.uuid4()

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run when LLM ends running."""
        if self.ctx.data_engine:
            generations = getattr(response, "generations", [])
            flat_generations = []
            if generations:
                for gen in generations[0]:
                    message = getattr(gen, "message", None)
                    if message:
                        flat_generations.append(self._serialize_message(message))
                    else:
                        flat_generations.append({"text": getattr(gen, "text", "")})

            span_data = self.running_spans.pop(run_id, {})
            messages = span_data.get("messages", [])
            prompts = span_data.get("prompts", [])
            name = span_data.get("name", "llm")
            start_time = span_data.get("start_time")
            duration = time.time() - start_time if start_time else None

            payload = {"response": flat_generations}
            if messages:
                payload["messages"] = messages
            if prompts:
                payload["prompts"] = prompts

            # Extract token usage if available from LLM output
            usage_data = None
            if hasattr(response, "llm_output") and isinstance(response.llm_output, dict):
                usage_data = response.llm_output.get("token_usage") or response.llm_output.get(
                    "usage_metadata"
                )
            if not usage_data and generations:
                for gen in generations[0]:
                    msg = getattr(gen, "message", None)
                    if msg:
                        if hasattr(msg, "usage_metadata") and msg.usage_metadata:
                            usage_data = msg.usage_metadata
                            break
                        if hasattr(msg, "response_metadata") and isinstance(
                            msg.response_metadata, dict
                        ):
                            usage_data = msg.response_metadata.get(
                                "usage_metadata"
                            ) or msg.response_metadata.get("token_usage")
                            if usage_data:
                                break
            if usage_data:
                payload["token_usage"] = usage_data

            self.ctx.data_engine.record_trace(
                type="llm_call",
                name=name,
                payload=payload,
                trace_id=run_id,
                parent_trace_id=self._resolve_parent_id(parent_run_id),
                status="success",
                duration=duration,
                step_id=self.ctx.data_engine.current_step_id,
            )

            native_thoughts = []
            raw_thoughts = []
            for gen_item in flat_generations:
                content = gen_item.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "thinking" and block.get("thinking"):
                                native_thoughts.append(str(block.get("thinking")).strip())
                            elif block.get("type") == "text" and block.get("text"):
                                raw_thoughts.append(str(block.get("text")).strip())
                elif isinstance(content, str) and content.strip():
                    raw_thoughts.append(content.strip())
                elif gen_item.get("text") and str(gen_item.get("text")).strip():
                    raw_thoughts.append(str(gen_item.get("text")).strip())

            if native_thoughts:
                self.ctx.data_engine.record_trace(
                    type="thinking",
                    name="thinking",
                    payload={"thought": "\n\n".join(native_thoughts)},
                    trace_id=self._safe_uuid5(run_id, "thinking"),
                    parent_trace_id=run_id,
                    status="success",
                    step_id=self.ctx.data_engine.current_step_id,
                )
            if raw_thoughts:
                self.ctx.data_engine.record_trace(
                    type="raw_thinking",
                    name="raw_thinking",
                    payload={"thought": "\n\n".join(raw_thoughts)},
                    trace_id=self._safe_uuid5(run_id, "raw_thinking"),
                    parent_trace_id=run_id,
                    status="success",
                    step_id=self.ctx.data_engine.current_step_id,
                )

    def on_llm_error(
        self,
        error: Any,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run when LLM errors."""
        if self.ctx.data_engine:
            span_data = self.running_spans.pop(run_id, {})
            messages = span_data.get("messages", [])
            prompts = span_data.get("prompts", [])
            name = span_data.get("name", "llm")
            start_time = span_data.get("start_time")
            duration = time.time() - start_time if start_time else None

            payload = {"error": serialize_error(error)}
            if messages:
                payload["messages"] = messages
            if prompts:
                payload["prompts"] = prompts

            self.ctx.data_engine.record_trace(
                type="llm_call",
                name=name,
                payload=payload,
                trace_id=run_id,
                parent_trace_id=self._resolve_parent_id(parent_run_id),
                status="failed",
                duration=duration,
                step_id=self.ctx.data_engine.current_step_id,
            )

    def _serialize_message(self, msg: BaseMessage) -> dict[str, Any]:
        return {
            "type": msg.type,
            "content": msg.content,
            "additional_kwargs": msg.additional_kwargs,
        }


def trace(
    type: str,
    name: str | None = None,
    ctx: ArtemisContext | None = None,
    serializer: Callable[[Any], dict[str, Any]] | None = None,
):
    """Decorator to trace execution of tools and agents in Data Engine.

    Args:
        type: "agent" or "tool"
        name: Optional name of the trace. Defaults to function name.
        ctx: Optional ArtemisContext. If not provided, tries to find it in
          arguments.
        serializer: Optional function to customize how result is stored.
    """

    def decorator(func):
        trace_name = name or func.__name__
        sig = inspect.signature(func)

        def _get_ctx(*args, **kwargs) -> ArtemisContext | None:
            if ctx:
                return ctx
            # Try to find in args
            for arg in args:
                if isinstance(arg, ArtemisContext):
                    return arg
                if hasattr(arg, "ctx") and isinstance(getattr(arg, "ctx"), ArtemisContext):
                    return getattr(arg, "ctx")
            # Try to find in kwargs
            for v in kwargs.values():
                if isinstance(v, ArtemisContext):
                    return v
                if hasattr(v, "ctx") and isinstance(getattr(v, "ctx"), ArtemisContext):
                    return getattr(v, "ctx")
            return None

        def _record(
            ctx_obj,
            args,
            kwargs,
            result,
            status,
            duration,
            trace_id,
            parent_trace_id,
            error=None,
        ):
            if not ctx_obj or not ctx_obj.data_engine:
                return

            try:
                bound = sig.bind(*args, **kwargs)
                filtered_args = {}
                for k, v in bound.arguments.items():
                    if k not in ("self", "ctx", "state", "tool_call_id"):
                        filtered_args[k] = smart_serialize(v)
                payload = {"args": filtered_args}
            except Exception:
                filtered_kwargs = {
                    k: v for k, v in kwargs.items() if k not in ("state", "tool_call_id")
                }
                payload = {"args": {k: smart_serialize(v) for k, v in filtered_kwargs.items()}}
                if args:
                    payload["positional_args"] = [smart_serialize(a) for a in args]

            if status == "success":
                if serializer:
                    payload["result"] = serializer(result)
                else:
                    payload["result"] = smart_serialize(result)
            elif status == "failed":
                payload["error"] = serialize_error(error)

            step_id = None
            if (
                hasattr(ctx_obj.data_engine, "current_step_id")
                and ctx_obj.data_engine.current_step_id
            ):
                step_id = ctx_obj.data_engine.current_step_id
            else:
                try:
                    bound = sig.bind(*args, **kwargs)
                    state_arg = bound.arguments.get("state")
                    if (
                        state_arg
                        and hasattr(state_arg, "current_step_id")
                        and state_arg.current_step_id
                    ):
                        step_id = UUID(str(state_arg.current_step_id))
                except (TypeError, ValueError, AttributeError):
                    pass

            ctx_obj.data_engine.record_trace(
                type=type,
                name=trace_name,
                payload=payload,
                status=status,
                duration=duration,
                trace_id=trace_id,
                parent_trace_id=parent_trace_id,
                step_id=step_id,
            )

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            ctx_obj = _get_ctx(*args, **kwargs)
            start_time = time.time()

            trace_id = uuid.uuid4()
            parent_id = CURRENT_TRACE_ID.get()

            token = CURRENT_TRACE_ID.set(trace_id)
            token_name = CURRENT_NODE_NAME.set(trace_name)

            # Record trace at start with 'running' status
            _record(ctx_obj, args, kwargs, None, "running", 0.0, trace_id, parent_id)

            try:
                result = await func(*args, **kwargs)
                duration = time.time() - start_time
                _record(
                    ctx_obj,
                    args,
                    kwargs,
                    result,
                    "success",
                    duration,
                    trace_id,
                    parent_id,
                )
                return result
            except Exception as e:
                duration = time.time() - start_time
                _record(
                    ctx_obj,
                    args,
                    kwargs,
                    None,
                    "failed",
                    duration,
                    trace_id,
                    parent_id,
                    error=e,
                )
                raise e
            finally:
                CURRENT_TRACE_ID.reset(token)
                CURRENT_NODE_NAME.reset(token_name)

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            ctx_obj = _get_ctx(*args, **kwargs)
            start_time = time.time()

            trace_id = uuid.uuid4()
            parent_id = CURRENT_TRACE_ID.get()

            token = CURRENT_TRACE_ID.set(trace_id)
            token_name = CURRENT_NODE_NAME.set(trace_name)

            # Record trace at start with 'running' status
            _record(ctx_obj, args, kwargs, None, "running", 0.0, trace_id, parent_id)

            try:
                result = func(*args, **kwargs)
                duration = time.time() - start_time
                _record(
                    ctx_obj,
                    args,
                    kwargs,
                    result,
                    "success",
                    duration,
                    trace_id,
                    parent_id,
                )
                return result
            except Exception as e:
                duration = time.time() - start_time
                _record(
                    ctx_obj,
                    args,
                    kwargs,
                    None,
                    "failed",
                    duration,
                    trace_id,
                    parent_id,
                    error=e,
                )
                raise e
            finally:
                CURRENT_TRACE_ID.reset(token)
                CURRENT_NODE_NAME.reset(token_name)

        if inspect.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


class TraceSpan:
    """Context manager to create a child trace span with dynamic payload and proactive state."""

    def __init__(
        self,
        name: str,
        trace_type: str = "span",
        ctx: ArtemisContext | None = None,
        serializer: Callable[[Any], dict[str, Any]] | None = None,
    ):
        self.name = name
        self.trace_type = trace_type
        self.ctx = ctx
        self.serializer = serializer
        self.start_time = None
        self.trace_id = uuid.uuid4()
        self.parent_id = None
        self.token = None
        self.status = "success"
        self.result: Any = None
        self.error: Any = None
        self.payload: dict[str, Any] = {}

    def __enter__(self):
        self.start_time = time.time()
        self.parent_id = CURRENT_TRACE_ID.get()
        self.token = CURRENT_TRACE_ID.set(self.trace_id)

        if self.ctx and self.ctx.data_engine:
            step_id = getattr(self.ctx.data_engine, "current_step_id", None)
            self.ctx.data_engine.record_trace(
                type=self.trace_type,
                name=self.name,
                payload={"status": "running", **self.payload},
                status="running",
                parent_trace_id=self.parent_id,
                step_id=step_id,
                trace_id=self.trace_id,
            )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration = time.time() - self.start_time
        CURRENT_TRACE_ID.reset(self.token)

        if exc_type:
            self.status = "failed"
            self.error = exc_val

        if self.ctx and self.ctx.data_engine:
            final_payload = dict(self.payload)
            if self.status == "success":
                if self.serializer and self.result:
                    final_payload["result"] = self.serializer(self.result)
                elif self.result:
                    final_payload["result"] = smart_serialize(self.result)
            else:
                final_payload["error"] = serialize_error(self.error)

            step_id = getattr(self.ctx.data_engine, "current_step_id", None)
            self.ctx.data_engine.record_trace(
                type=self.trace_type,
                name=self.name,
                payload=final_payload,
                status=self.status,
                duration=duration,
                trace_id=self.trace_id,
                parent_trace_id=self.parent_id,
                step_id=step_id,
            )


def trace_langchain_tool(tool: BaseTool, ctx: ArtemisContext) -> BaseTool:
    """Wraps a LangChain tool to trace its execution.

    [REFACTORED] Tracing is now completely delegated to
    DataEngineCallbackHandler infrastructure.
    """
    return tool
