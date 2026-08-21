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

"""Universal LLM Service for Artemis.

Provides connection-pooled model instantiation, robust retries,
thought stream recording, and role-based dynamic dispatching.
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from contextvars import ContextVar, Token
from dataclasses import replace
import functools
import logging
from pathlib import Path
import random
import re
import sys
import time
from typing import Any, Literal, TypeVar, overload
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel

from artemis.config import (
    AgentNode,
    AgentNodeWithFallback,
    LLMUtilsNode,
    LLMUtilsNodeWithFallback,
    LLMWithFallback,
    get_default_llm_config,
    settings,
)
from artemis.context import ArtemisContext
from artemis.data_engine.trace import CURRENT_TRACE_ID, DataEngineCallbackHandler
from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider
from artemis.utils.logger import get_logger

# Logger for internal messages
llm_logger = logging.getLogger(__name__)
# Logger for user-facing messages
user_messages_logger = get_logger(__name__)

T = TypeVar("T")

# Retained as an override seam for unit tests. At runtime the active engine must
# be resolved dynamically because DataEngine.start_session() updates the module
# global after this module has already been imported.
_CURRENT_DATA_ENGINE = None

# Provider retry logs do not carry an Artemis trace id.  Keep the active model
# request in async-local state so observable SDK retries and the terminal error
# are emitted as one lifecycle, even when several model calls run concurrently.
_ACTIVE_LLM_REQUEST: ContextVar[dict[str, Any] | None] = ContextVar(
    "active_llm_request", default=None
)


def _provider_name_from_call(args: tuple[Any, ...]) -> str | None:
    wrapper = args[0] if args else None
    endpoint = getattr(wrapper, "endpoint", None)
    provider = getattr(endpoint, "provider", None)
    if provider is None:
        return None
    return str(getattr(provider, "value", provider))


def _begin_llm_request(args: tuple[Any, ...]) -> Token:
    return _ACTIVE_LLM_REQUEST.set(
        {
            "request_id": str(uuid4()),
            "provider": _provider_name_from_call(args),
            "started_at": time.time(),
            "retries": [],
        }
    )


def _get_current_data_engine():
    if _CURRENT_DATA_ENGINE is not None:
        return _CURRENT_DATA_ENGINE
    engine_module = sys.modules.get("artemis.data_engine.engine")
    return getattr(engine_module, "_CURRENT_DATA_ENGINE", None) if engine_module else None


def _record_llm_retry(
    error: str,
    delay: float,
    *,
    attempt: int | None = None,
    max_retries: int | None = None,
    provider: str | None = None,
    source: str = "artemis",
) -> None:
    """Persist and publish one recoverable LLM retry for UI transparency."""
    request = _ACTIVE_LLM_REQUEST.get()
    timestamp = time.time()
    request_id = request.get("request_id") if request else None
    provider = provider or (request.get("provider") if request else None)

    engine = _get_current_data_engine()
    if not engine or not getattr(engine, "current_session_id", None):
        return

    error_text = str(error)
    if len(error_text) > 1000:
        error_text = error_text[:1000] + "... [Truncated by LLM Retry Telemetry]"

    payload = {
        "error": error_text,
        "delay": float(delay),
        "attempt": attempt,
        "max_retries": max_retries,
        "provider": provider,
        "source": source,
        "recoverable": True,
        "request_id": request_id,
        "scheduled_at": timestamp,
    }
    payload = {key: value for key, value in payload.items() if value is not None}
    step_id = getattr(engine, "current_step_id", None)
    parent_trace_id = CURRENT_TRACE_ID.get()
    try:
        trace_id = engine.record_trace(
            type="llm_call",
            name="llm_retry",
            payload=payload,
            step_id=step_id,
            parent_trace_id=parent_trace_id,
            status="retrying",
        )
    except Exception as trace_error:
        llm_logger.warning("Failed to persist LLM retry trace: %s", trace_error)
        trace_id = None

    if request is not None:
        request["retries"].append(
            {
                **payload,
                "trace_id": str(trace_id) if trace_id else None,
                "timestamp": timestamp,
            }
        )

    try:
        engine._publish(
            "llm_retrying",
            {
                **payload,
                "step_id": str(step_id) if step_id else None,
                "trace_id": str(trace_id) if trace_id else None,
                "timestamp": timestamp,
            },
        )
    except Exception as publish_error:
        # Retry visibility is best-effort and must never break the LLM retry itself.
        llm_logger.warning("Failed to publish LLM retry event: %s", publish_error)


class _ProviderRetryTelemetryHandler(logging.Handler):
    """Turn provider SDK retry log records into structured Artemis telemetry."""

    _DELAY_PATTERN = re.compile(r"\bin\s+(?P<delay>\d+(?:\.\d+)?)\s+seconds?\b", re.IGNORECASE)
    _ERROR_PATTERN = re.compile(r"\bas it raised\s+(?P<error>.+)$", re.IGNORECASE)
    _RETRYABLE_MARKERS = (
        "503",
        "429",
        "unavailable",
        "resource_exhausted",
        "high demand",
        "quota",
        "throttled",
        "overloaded",
    )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            request = _ACTIVE_LLM_REQUEST.get()
            if not request or request.get("provider") != ModelProvider.GOOGLE.value:
                return

            message = record.getMessage()
            lowered = message.lower()
            if "retrying" not in lowered or not any(
                marker in lowered for marker in self._RETRYABLE_MARKERS
            ):
                return

            delay_match = self._DELAY_PATTERN.search(message)
            error_match = self._ERROR_PATTERN.search(message)
            delay = float(delay_match.group("delay")) if delay_match else 0.0
            error = error_match.group("error").strip() if error_match else message
            _record_llm_retry(
                error,
                delay,
                provider=request["provider"],
                source="provider_sdk",
            )
        except Exception:
            # Telemetry must never interfere with the provider's own retry.
            self.handleError(record)


def _install_provider_retry_telemetry() -> None:
    provider_logger = logging.getLogger("google_genai._api_client")
    if any(
        isinstance(handler, _ProviderRetryTelemetryHandler)
        for handler in provider_logger.handlers
    ):
        return
    provider_logger.addHandler(_ProviderRetryTelemetryHandler(level=logging.INFO))


_install_provider_retry_telemetry()


def _handle_llm_pause_and_resume(last_error: Exception) -> Path:
    """Handles pausing task execution upon persistent failure until resumed."""
    err_msg = str(last_error)
    if len(err_msg) > 1000:
        err_msg = err_msg[:1000] + "... [Truncated by LLM Wrapper]"

    llm_logger.warning(f"LLM Error: {err_msg}. Pausing execution to wait for resume signal...")
    pause_file = Path(settings.TRACES_PATH).parent / ".artemis_paused"
    try:
        pause_file.write_text(f"LLM Error: {err_msg}")
    except Exception:
        pass

    current_engine = _get_current_data_engine()
    if current_engine:
        step_id = getattr(current_engine, "current_step_id", None)
        timestamp = time.time()
        request = _ACTIVE_LLM_REQUEST.get()
        failure_payload: dict[str, Any] = {"error": err_msg, "pause": True}
        if request:
            failure_payload.update(
                {
                    "request_id": request["request_id"],
                    "provider": request.get("provider"),
                    "waited_seconds": max(0.0, timestamp - request["started_at"]),
                    # Only SDK-observed retries are included. Other providers
                    # may retry internally, but their attempt data is opaque.
                    "retries": list(request["retries"]),
                }
            )
            failure_payload = {
                key: value for key, value in failure_payload.items() if value is not None
            }
        try:
            # Persist exhausted LLM retries as a failed call so the same error
            # card is available both in the live stream and after a refresh.
            current_engine.record_trace(
                type="llm_call",
                name="llm_pause",
                payload=failure_payload,
                step_id=step_id,
                status="failed",
            )
        except Exception as trace_error:
            llm_logger.warning("Failed to persist paused LLM error trace: %s", trace_error)

        current_engine._publish(
            "task_paused",
            {
                **failure_payload,
                "step_id": str(step_id) if step_id else None,
                "timestamp": timestamp,
            },
        )

    return pause_file


def robust_retry_async(func: Callable) -> Callable:
    """Decorator to add robust retry and pause/resume logic to an async LLM call."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        while True:
            request_token = _begin_llm_request(args)
            max_retries = 3
            retry_delay = 0.0
            last_error = None
            for attempt in range(max_retries):
                try:
                    result = await func(*args, **kwargs)
                    _ACTIVE_LLM_REQUEST.reset(request_token)
                    return result
                except Exception as e:
                    last_error = e
                    err_str = str(e).lower()
                    if attempt == max_retries - 1:
                        break
                    if attempt > 0:
                        retry_delay = 0.5 * (2 ** (attempt - 1)) + random.uniform(0.1, 0.3)
                    if any(
                        err in err_str
                        for err in [
                            "503",
                            "throttled",
                            "overloaded",
                            "429",
                            "quota",
                            "resource_exhausted",
                        ]
                    ):
                        retry_delay = max(retry_delay, 10.0)
                    if retry_delay > 0:
                        llm_logger.warning(f"LLM call failed, retrying in {retry_delay:.2f}s...")

                        await asyncio.sleep(retry_delay)
                except BaseException:
                    _ACTIVE_LLM_REQUEST.reset(request_token)
                    raise

            try:
                pause_file = _handle_llm_pause_and_resume(last_error)
            finally:
                _ACTIVE_LLM_REQUEST.reset(request_token)
            while pause_file.exists():
                await asyncio.sleep(1)

            llm_logger.info("Resume signal received, retrying LLM call...")

            current_engine = _get_current_data_engine()
            if current_engine:
                current_engine._publish("task_resumed", {})

    return wrapper


def robust_retry_astream(func: Callable) -> Callable:
    """Decorator to add robust retry and pause/resume logic to an async stream LLM call."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        while True:
            request_token = _begin_llm_request(args)
            max_retries = 3
            retry_delay = 0.0
            last_error = None
            for attempt in range(max_retries):
                try:
                    gen = func(*args, **kwargs)
                    first = await gen.__anext__()
                    yield first
                    async for chunk in gen:
                        yield chunk
                    _ACTIVE_LLM_REQUEST.reset(request_token)
                    return
                except Exception as e:
                    last_error = e
                    err_str = str(e).lower()
                    if attempt == max_retries - 1:
                        break
                    if attempt > 0:
                        retry_delay = 0.5 * (2 ** (attempt - 1)) + random.uniform(0.1, 0.3)
                    if any(
                        err in err_str
                        for err in [
                            "503",
                            "throttled",
                            "overloaded",
                            "429",
                            "quota",
                            "resource_exhausted",
                        ]
                    ):
                        retry_delay = max(retry_delay, 10.0)
                    if retry_delay > 0:
                        llm_logger.warning(
                            f"LLM stream handshake throttled, retrying in {retry_delay:.2f}s..."
                        )

                        await asyncio.sleep(retry_delay)
                except BaseException:
                    _ACTIVE_LLM_REQUEST.reset(request_token)
                    raise

            try:
                pause_file = _handle_llm_pause_and_resume(last_error)
            finally:
                _ACTIVE_LLM_REQUEST.reset(request_token)
            while pause_file.exists():
                await asyncio.sleep(1)

            llm_logger.info("Resume signal received, retrying LLM stream...")

            current_engine = _get_current_data_engine()
            if current_engine:
                current_engine._publish("task_resumed", {})

    return wrapper


def _inject_parent_trace_id(trace_id, *args, **kwargs):
    config = kwargs.get("config") or {}
    if not kwargs.get("config") and len(args) > 1:
        args_list = list(args)
        if isinstance(args_list[1], dict):
            config = args_list[1]
            args_list[1] = config
            args = tuple(args_list)

    if "metadata" not in config:
        config["metadata"] = {}
    config["metadata"]["parent_trace_id"] = str(trace_id)
    kwargs["config"] = config
    return args, kwargs


class RobustChatModelWrapper:
    """Universal LangChain Runnable wrapper ensuring robust execution and telemetry."""

    def __init__(
        self,
        base_model: BaseChatModel,
        ctx: ArtemisContext = None,
        endpoint: ModelEndpoint | None = None,
    ):
        self.base_model = base_model
        self.ctx = ctx
        self.endpoint = endpoint

    def __getattr__(self, item: str):
        return getattr(self.base_model, item)

    def bind_tools(self, *args, **kwargs):
        # Extract tools list from positional args or keyword args
        tools_list = None
        other_args = list(args)
        if other_args:
            tools_list = other_args.pop(0)
        elif "tools" in kwargs:
            tools_list = kwargs.pop("tools")

        processed_tools = list(tools_list) if tools_list is not None else []

        # Determine if the underlying model provider is Google / Gemini
        is_google_provider = False
        if self.endpoint is not None:
            is_google_provider = self.endpoint.provider in (
                ModelProvider.GOOGLE,
                ModelProvider.GEMINI,
            )
        else:
            base_model_cls = getattr(self.base_model, "__class__", None)
            base_model_name = base_model_cls.__name__ if base_model_cls else ""
            is_google_provider = (
                "Google" in base_model_name
                or "Gemini" in base_model_name
                or "VertexAI" in base_model_name
            )

        has_explicit_google_search = any(
            isinstance(t, dict) and "google_search" in t for t in processed_tools
        )
        should_ground = (
            self.endpoint is not None and self.endpoint.enable_grounding
        ) or has_explicit_google_search

        if is_google_provider:
            if should_ground:
                # Add native Google Search Grounding tool if not already present
                if not has_explicit_google_search:
                    processed_tools.append({"google_search": {}})

                # Ensure include_server_side_tool_invocations is True in tool_config
                # to satisfy Gemini API requirements when mixing built-in tools and function calling
                tool_config = kwargs.get("tool_config")
                if tool_config is None:
                    kwargs["tool_config"] = {"include_server_side_tool_invocations": True}
                elif isinstance(tool_config, dict):
                    tool_config = dict(tool_config)
                    tool_config.setdefault("include_server_side_tool_invocations", True)
                    kwargs["tool_config"] = tool_config
        else:
            # If not Gemini/Google (e.g. OpenAI, Anthropic, Ollama, OpenRouter, XAI):
            # Ignore grounding and filter out any Gemini-specific built-in dict tools to avoid provider errors
            processed_tools = [
                t for t in processed_tools if not (isinstance(t, dict) and "google_search" in t)
            ]

        bound = self.base_model.bind_tools(processed_tools, *other_args, **kwargs)
        return RobustChatModelWrapper(bound, self.ctx, endpoint=self.endpoint)

    def with_config(self, *args, **kwargs):
        return RobustChatModelWrapper(
            self.base_model.with_config(*args, **kwargs),
            self.ctx,
            endpoint=self.endpoint,
        )

    def with_structured_output(self, *args, **kwargs):
        if hasattr(self.base_model, "with_structured_output"):
            return RobustChatModelWrapper(
                self.base_model.with_structured_output(*args, **kwargs),
                self.ctx,
                endpoint=self.endpoint,
            )
        raise AttributeError(f"{type(self.base_model)} does not support with_structured_output")

    def __or__(self, other):
        return RobustChatModelWrapper(
            self.base_model.__or__(other), self.ctx, endpoint=self.endpoint
        )

    def __ror__(self, other):
        return RobustChatModelWrapper(
            self.base_model.__ror__(other), self.ctx, endpoint=self.endpoint
        )

    @robust_retry_async
    async def ainvoke(self, *args, **kwargs):
        trace_id = None
        if self.ctx and self.ctx.data_engine:
            trace_id = CURRENT_TRACE_ID.get()
        if trace_id:
            args, kwargs = _inject_parent_trace_id(trace_id, *args, **kwargs)
        return await self.base_model.ainvoke(*args, **kwargs)

    @robust_retry_astream
    async def astream(self, *args, **kwargs):
        trace_id = None
        if self.ctx and self.ctx.data_engine:
            trace_id = CURRENT_TRACE_ID.get()

        if trace_id:
            args, kwargs = _inject_parent_trace_id(trace_id, *args, **kwargs)

        stream_exec_id = uuid4()

        async for chunk in self.base_model.astream(*args, **kwargs):
            if trace_id and self.ctx and self.ctx.data_engine and chunk.content:
                text_to_stream = ""
                thinking_to_stream = ""
                if isinstance(chunk.content, str):
                    text_to_stream = chunk.content
                elif isinstance(chunk.content, list):
                    for item in chunk.content:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                text_to_stream += item.get("text", "")
                            elif item.get("type") == "thinking":
                                thinking_to_stream += item.get("thinking", "")

                if text_to_stream:
                    self.ctx.data_engine.stream_output(
                        stream_exec_id, text_to_stream, is_thinking=False
                    )
                if thinking_to_stream:
                    self.ctx.data_engine.stream_output(
                        stream_exec_id, thinking_to_stream, is_thinking=True
                    )
            yield chunk


async def invoke_llm_with_timeout_message[T](
    llm_call: Coroutine[Any, Any, T],
    timeout_seconds: int = 10,
    hard_timeout: int = 180,
) -> T:
    """Send an LLM call and display a countdown / timeout message if delayed."""
    llm_task = asyncio.create_task(llm_call)
    waiter_task = asyncio.create_task(asyncio.sleep(timeout_seconds))

    done, _ = await asyncio.wait({llm_task, waiter_task}, return_when=asyncio.FIRST_COMPLETED)

    if llm_task in done:
        waiter_task.cancel()
        return llm_task.result()
    else:
        user_messages_logger.info("Waiting for LLM call response...")
        start_time = asyncio.get_event_loop().time()

        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(llm_task), timeout=1.0)
            except TimeoutError:
                if llm_task.done():
                    return llm_task.result()

                pause_file = Path(settings.TRACES_PATH).parent / ".artemis_paused"
                if pause_file.exists():
                    start_time = asyncio.get_event_loop().time()
                    continue

                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > max(0, hard_timeout - timeout_seconds):
                    user_messages_logger.error(f"LLM call timed out after {hard_timeout} seconds.")
                    raise TimeoutError(f"LLM call timed out after {hard_timeout} seconds.")


# Backward compatible factory functions delegating to ModelFactory
def get_google_llm(
    model_name: str = "gemini-3.7-flash",
    temperature: float | None = None,
    timeout: float | None = None,
    thinking_budget: int | None = None,
    thinking_level: str | None = "medium",
    include_thoughts: bool | None = None,
    enable_grounding: bool = False,
) -> BaseChatModel:
    ep = ModelEndpoint(
        provider=ModelProvider.GOOGLE,
        model_name=model_name,
        temperature=temperature or 0.0,
        timeout_seconds=timeout or 60.0,
        thinking_budget=thinking_budget,
        thinking_level=thinking_level,
        include_thoughts=include_thoughts,
        enable_grounding=enable_grounding,
    )
    return ModelFactory.create_model(ep)


def get_vertex_llm(
    model_name: str = "gemini-3.7-flash",
    temperature: float | None = None,
    timeout: float | None = None,
    thinking_budget: int | None = None,
) -> BaseChatModel:
    ep = ModelEndpoint(
        provider=ModelProvider.VERTEX_AI,
        model_name=model_name,
        temperature=temperature or 0.0,
        timeout_seconds=timeout or 60.0,
        thinking_budget=thinking_budget,
    )
    return ModelFactory.create_model(ep)


def get_openai_llm(
    model_name: str = "o3",
    temperature: float | None = None,
    timeout: float | None = None,
) -> BaseChatModel:
    ep = ModelEndpoint(
        provider=ModelProvider.OPENAI,
        model_name=model_name,
        temperature=temperature or 0.0,
        timeout_seconds=timeout or 60.0,
    )
    return ModelFactory.create_model(ep)


def get_openrouter_llm(
    model_name: str,
    temperature: float | None = None,
    timeout: float | None = None,
) -> BaseChatModel:
    ep = ModelEndpoint(
        provider=ModelProvider.OPENROUTER,
        model_name=model_name,
        temperature=temperature or 0.0,
        timeout_seconds=timeout or 60.0,
    )
    return ModelFactory.create_model(ep)


def get_grok_llm(
    model_name: str,
    temperature: float | None = None,
    timeout: float | None = None,
) -> BaseChatModel:
    ep = ModelEndpoint(
        provider=ModelProvider.XAI,
        model_name=model_name,
        temperature=temperature or 0.0,
        timeout_seconds=timeout or 60.0,
    )
    return ModelFactory.create_model(ep)


def get_anthropic_llm(
    model_name: str,
    temperature: float | None = None,
    timeout: float | None = None,
    thinking_budget: int | None = None,
    reasoning_effort: str | None = None,
) -> BaseChatModel:
    ep = ModelEndpoint(
        provider=ModelProvider.ANTHROPIC,
        model_name=model_name,
        temperature=temperature or 0.0,
        timeout_seconds=timeout or 60.0,
        thinking_budget=thinking_budget,
        reasoning_effort=reasoning_effort,
    )
    return ModelFactory.create_model(ep)


def get_cached_raw_model(
    provider: str,
    model_name: str,
    temperature: float | None = None,
    timeout: float | None = None,
    thinking_budget: int | None = None,
    thinking_level: str | None = None,
    include_thoughts: bool | None = None,
    reasoning_effort: str | None = None,
    enable_grounding: bool = False,
) -> BaseChatModel:
    """Retrieves or instantiates a cached raw LangChain chat model."""
    ep = ModelEndpoint(
        provider=ModelProvider.from_string(provider),
        model_name=model_name,
        temperature=temperature or 0.0,
        timeout_seconds=timeout or 60.0,
        thinking_budget=thinking_budget,
        thinking_level=thinking_level,
        include_thoughts=include_thoughts,
        reasoning_effort=reasoning_effort,
        enable_grounding=enable_grounding,
    )
    return ModelFactory.get_model(ep)


def _resolve_endpoint(
    ctx: ArtemisContext,
    name: str,
    is_utils: bool = False,
    use_fallback: bool = False,
) -> ModelEndpoint:
    """Cleanly resolves a ModelEndpoint from context router or llm_config."""
    # 1. Prefer dynamic ModelRouter if attached to context
    if getattr(ctx, "model_router", None) is not None:
        role_name = str(name).lower()
        if use_fallback:
            fallbacks = ctx.model_router.get_fallbacks(role_name)
            if fallbacks:
                return fallbacks[0]
        return ctx.model_router.get_endpoint(role_name)

    # 2. Fall back to context llm_config
    if getattr(ctx, "llm_config", None) is None:
        try:
            ctx.llm_config = get_default_llm_config()
        except Exception:
            pass

    cfg = ctx.llm_config.get_utils(name) if is_utils else ctx.llm_config.get_agent(name)

    if use_fallback:
        if isinstance(cfg, LLMWithFallback) or (hasattr(cfg, "fallback") and cfg.fallback):
            cfg = cfg.fallback
        else:
            raise ValueError(f"LLM configuration for '{name}' has no fallback!")

    def _get_val(obj, attr, expected_type):
        val = getattr(obj, attr, None)
        return val if isinstance(val, expected_type) else None

    provider_val = getattr(cfg, "provider", "google")
    model_val = getattr(cfg, "model", "gemini-3.7-flash")

    return ModelEndpoint(
        provider=ModelProvider.from_string(provider_val),
        model_name=str(model_val),
        temperature=_get_val(cfg, "temperature", (int, float)) or 0.0,
        timeout_seconds=_get_val(cfg, "timeout", (int, float)) or 60.0,
        thinking_budget=_get_val(cfg, "thinking_budget", int),
        thinking_level=_get_val(cfg, "thinking_level", str),
        reasoning_effort=_get_val(cfg, "reasoning_effort", str),
        include_thoughts=_get_val(cfg, "include_thoughts", bool),
        enable_grounding=_get_val(cfg, "enable_grounding", bool) or False,
    )


@overload
def get_llm(
    ctx: ArtemisContext,
    name: AgentNodeWithFallback,
    *,
    use_fallback: bool = False,
    temperature: float | None = None,
) -> BaseChatModel: ...


@overload
def get_llm(
    ctx: ArtemisContext,
    name: LLMUtilsNode,
    *,
    is_utils: Literal[True],
    temperature: float | None = None,
) -> BaseChatModel: ...


@overload
def get_llm(
    ctx: ArtemisContext,
    name: LLMUtilsNodeWithFallback,
    *,
    is_utils: Literal[True],
    use_fallback: bool = False,
    temperature: float | None = None,
) -> BaseChatModel: ...


def get_llm(
    ctx: ArtemisContext,
    name: AgentNode | LLMUtilsNode | AgentNodeWithFallback,
    is_utils: bool = False,
    use_fallback: bool = False,
    temperature: float | None = None,
) -> BaseChatModel:
    """Resolves and instantiates the appropriate LLM wrapper for the given agent role."""
    endpoint = _resolve_endpoint(ctx, str(name), is_utils=is_utils, use_fallback=use_fallback)
    if temperature is not None:
        endpoint = replace(endpoint, temperature=temperature)
    raw_model = ModelFactory.get_model(endpoint)

    handler = DataEngineCallbackHandler(ctx)
    bound_model = raw_model.with_config(callbacks=[handler])

    return RobustChatModelWrapper(bound_model, ctx, endpoint=endpoint)  # type: ignore


async def with_fallback[T](
    main_call: Callable[[], Awaitable[T]],
    fallback_call: Callable[[], Awaitable[T]],
    none_should_fallback: bool = True,
) -> T:
    try:
        result = await main_call()
        if result is None and none_should_fallback:
            llm_logger.warning("Main LLM inference returned None. Falling back...")
            return await fallback_call()
        return result
    except Exception as e:
        llm_logger.warning(f"❗ Main LLM inference failed: {e}. Falling back...")
        return await fallback_call()
