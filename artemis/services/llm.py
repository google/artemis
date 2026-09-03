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
import functools
import logging
from pathlib import Path
import re
import sys
import time
from typing import Any, Literal, TypeVar, overload
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from artemis.config import (
    PAUSE_FILE,
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
from artemis.llm.reliability import (
    CircuitBreaker,
    FailureCategory,
    LLMCallError,
    LLMExhaustedError,
    LLMPermanentError,
    classify_failure,
    retry_policy_for,
)
from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider
from artemis.services.token_meter import record_llm_usage
from artemis.llm.structured import (
    ParseFailure,
    StructuredOutputError,
    content_to_text,
    parse_structured,
)
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


def _begin_llm_request(provider: str | None) -> Token:
    return _ACTIVE_LLM_REQUEST.set(
        {
            "request_id": str(uuid4()),
            "provider": provider,
            "started_at": time.time(),
            "retries": [],
        }
    )


# Whether the current async context has a configured fallback model waiting
# behind this call (set by with_fallback).  When a fallback exists, exhausting
# retries hands over to it immediately instead of pausing the whole task.
_FALLBACK_AVAILABLE: ContextVar[bool] = ContextVar("llm_fallback_available", default=False)

# Shared circuit breaker keyed by "provider:model".  Transient failures open
# it; while open, concurrent calls wait out the cooldown instead of hammering
# a provider that is already melting down.
_ENDPOINT_BREAKER = CircuitBreaker(threshold=3, cooldown_seconds=30.0)

# Endpoints observed to not support streaming.  Detected once, loudly, then
# remembered so subsequent calls go straight to non-streaming invocation.
_NON_STREAMING_ENDPOINTS: set[str] = set()

# Total retry limit when failures switch categories within one recovery cycle.
_MAX_TOTAL_RETRY_ATTEMPTS = 8


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


def _record_llm_event(name: str, payload: dict[str, Any], *, status: str = "retrying") -> None:
    """Persist and publish one degradation/lifecycle event on the LLM IO path.

    Every degradation (fallback, stream downgrade, stream reset, giving up)
    must be observable: WARNING-level logging is handled by callers, this
    records the structured trace + live event, best-effort.
    """
    request = _ACTIVE_LLM_REQUEST.get()
    engine = _get_current_data_engine()
    if not engine or not getattr(engine, "current_session_id", None):
        return

    full_payload = dict(payload)
    if request is not None:
        full_payload.setdefault("request_id", request.get("request_id"))
        if request.get("provider"):
            full_payload.setdefault("provider", request.get("provider"))
    full_payload = {key: value for key, value in full_payload.items() if value is not None}

    step_id = getattr(engine, "current_step_id", None)
    trace_id = None
    try:
        trace_id = engine.record_trace(
            type="llm_call",
            name=name,
            payload=full_payload,
            step_id=step_id,
            parent_trace_id=CURRENT_TRACE_ID.get(),
            status=status,
        )
    except Exception as trace_error:
        llm_logger.warning("Failed to persist LLM event trace %s: %s", name, trace_error)
    try:
        engine._publish(
            name,
            {
                **full_payload,
                "step_id": str(step_id) if step_id else None,
                "trace_id": str(trace_id) if trace_id else None,
                "timestamp": time.time(),
            },
        )
    except Exception as publish_error:
        llm_logger.warning("Failed to publish LLM event %s: %s", name, publish_error)


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
        isinstance(handler, _ProviderRetryTelemetryHandler) for handler in provider_logger.handlers
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
    pause_file = PAUSE_FILE
    try:
        pause_file.write_text(f"LLM Error: {err_msg}", encoding="utf-8")
    except (OSError, UnicodeError):
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


async def _wait_for_resume(pause_file: Path) -> bool:
    """Wait for the pause file to be cleared. Returns False on deadline."""
    deadline = float(getattr(settings, "LLM_PAUSE_TIMEOUT_SECONDS", 0.0) or 0.0)
    waited = 0.0
    while pause_file.exists():
        if deadline > 0 and waited >= deadline:
            return False
        await asyncio.sleep(1)
        waited += 1
    return True


async def _wait_for_breaker(key: str) -> None:
    """Wait out an open circuit instead of hammering a melting provider."""
    waited = 0.0
    warned = False
    while not _ENDPOINT_BREAKER.allow(key):
        if not warned:
            warned = True
            llm_logger.warning(f"LLM circuit open for {key}; waiting for cooldown...")
        step = min(max(_ENDPOINT_BREAKER.open_remaining(key), 0.5), 2.0)
        await asyncio.sleep(step)
        waited += step
        if waited >= 120.0:
            # Never deadlock behind a stuck half-open trial; proceed anyway.
            break


async def _run_with_recovery[T](
    call_once: Callable[[], Awaitable[T]],
    *,
    provider: str | None = None,
    endpoint_key: str | None = None,
) -> T:
    """Execute one LLM call under the classified retry / pause / breaker policy.

    Failure handling is decided per category (see artemis.llm.reliability):

    - Retryable categories back off per policy; each retry is recorded for UI
      transparency via _record_llm_retry.
    - Non-retryable categories (auth, bad request) raise LLMPermanentError
      immediately: retrying cannot help and pausing would hang the task.
    - When retryable attempts are exhausted: if a fallback model is waiting
      (with_fallback), raise LLMExhaustedError so it takes over immediately;
      otherwise pause the task (bounded by settings.LLM_PAUSE_TIMEOUT_SECONDS)
      and retry from scratch on resume.
    """
    while True:
        request_token = _begin_llm_request(provider)
        last_error: Exception | None = None
        last_failure = None
        attempts: dict[FailureCategory, int] = {}
        pause_file: Path | None = None
        try:
            while True:
                if endpoint_key:
                    await _wait_for_breaker(endpoint_key)
                try:
                    result = await call_once()
                except (KeyboardInterrupt, asyncio.CancelledError):
                    raise
                except LLMCallError:
                    # Already classified terminal by a nested recovery layer.
                    raise
                except Exception as e:
                    failure = classify_failure(e)
                    if failure.category is FailureCategory.CANCELLED:
                        raise
                    if endpoint_key:
                        _ENDPOINT_BREAKER.record_failure(endpoint_key, failure)
                    if not failure.retryable:
                        llm_logger.warning(
                            f"LLM call failed permanently ({failure.category.value}): {e}"
                        )
                        _record_llm_event(
                            "llm_gave_up",
                            {
                                "error": str(e)[:1000],
                                "category": failure.category.value,
                                "retryable": False,
                            },
                            status="failed",
                        )
                        raise LLMPermanentError(str(e), failure=failure, cause=e) from e
                    last_error, last_failure = e, failure
                    attempt = attempts.get(failure.category, 0) + 1
                    attempts[failure.category] = attempt
                    policy = retry_policy_for(failure.category)
                    if attempt >= policy.max_attempts:
                        break
                    total_attempts = sum(attempts.values())
                    if total_attempts >= _MAX_TOTAL_RETRY_ATTEMPTS:
                        llm_logger.warning(
                            "LLM retry total across failure categories reached"
                            f" {total_attempts}; treating as exhausted."
                        )
                        break
                    delay = policy.delay_for(attempt)
                    _record_llm_retry(
                        str(e),
                        delay,
                        attempt=attempt,
                        max_retries=policy.max_attempts,
                        provider=provider,
                    )
                    llm_logger.warning(
                        f"LLM call failed ({failure.category.value}), retrying in {delay:.2f}s..."
                    )
                    await asyncio.sleep(delay)
                else:
                    if endpoint_key:
                        _ENDPOINT_BREAKER.record_success(endpoint_key)
                    return result

            # Retryable attempts exhausted.
            if _FALLBACK_AVAILABLE.get():
                _record_llm_event(
                    "llm_gave_up",
                    {
                        "error": str(last_error)[:1000],
                        "category": last_failure.category.value,
                        "retryable": True,
                        "handover": "fallback",
                    },
                    status="failed",
                )
                raise LLMExhaustedError(
                    str(last_error), failure=last_failure, cause=last_error
                ) from last_error
            pause_file = _handle_llm_pause_and_resume(last_error)
        finally:
            _ACTIVE_LLM_REQUEST.reset(request_token)

        if not await _wait_for_resume(pause_file):
            llm_logger.error(
                "LLM pause deadline"
                f" ({settings.LLM_PAUSE_TIMEOUT_SECONDS:.0f}s) exceeded; giving up."
            )
            try:
                pause_file.unlink(missing_ok=True)
            except OSError:
                pass
            raise LLMExhaustedError(
                str(last_error), failure=last_failure, cause=last_error
            ) from last_error

        llm_logger.info("Resume signal received, retrying LLM call...")
        current_engine = _get_current_data_engine()
        if current_engine:
            current_engine._publish("task_resumed", {})


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

    def _provider_value(self) -> str | None:
        provider = getattr(self.endpoint, "provider", None)
        if provider is None:
            return None
        return str(getattr(provider, "value", provider))

    def _endpoint_key(self) -> str:
        if self.endpoint is not None:
            return f"{self._provider_value()}:{self.endpoint.model_name}"
        return type(self.base_model).__name__

    def _traced_call(self, args: tuple, kwargs: dict) -> tuple[tuple, dict, Any]:
        trace_id = None
        if self.ctx and self.ctx.data_engine:
            trace_id = CURRENT_TRACE_ID.get()
        if trace_id:
            args, kwargs = _inject_parent_trace_id(trace_id, *args, **kwargs)
        return args, kwargs, trace_id

    def _meter_usage(self, response) -> None:
        """Record measured token usage for this call (M0 metering, best-effort)."""
        engine = self.ctx.data_engine if self.ctx else None
        if engine is None:
            engine = _get_current_data_engine()
        record_llm_usage(engine, response, source=self._endpoint_key())

    async def ainvoke(self, *args, **kwargs):
        args, kwargs, _ = self._traced_call(args, kwargs)
        response = await _run_with_recovery(
            functools.partial(self.base_model.ainvoke, *args, **kwargs),
            provider=self._provider_value(),
            endpoint_key=self._endpoint_key(),
        )
        self._meter_usage(response)
        return response

    async def complete(self, *args, **kwargs):
        """Single completion entry point: returns the full final message.

        Streaming is a transport detail handled internally — provider chunks
        are forwarded to the live UI as deltas and accumulated here, so
        downstream consumers only ever see one complete message (or a typed
        LLMCallError).  A mid-stream failure discards the partial output,
        signals the UI to drop it, and retries the whole call per policy;
        partial or duplicated chunks can never reach message history.
        """
        args, kwargs, _ = self._traced_call(args, kwargs)
        emit_deltas = bool(self.ctx and self.ctx.data_engine)
        response = await _run_with_recovery(
            functools.partial(self._complete_attempt, emit_deltas, *args, **kwargs),
            provider=self._provider_value(),
            endpoint_key=self._endpoint_key(),
        )
        self._meter_usage(response)
        return response

    def _emit_stream_delta(self, stream_exec_id, chunk) -> None:
        content = getattr(chunk, "content", None)
        if not content:
            return
        text_to_stream = ""
        thinking_to_stream = ""
        if isinstance(content, str):
            text_to_stream = content
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        text_to_stream += item.get("text", "")
                    elif item.get("type") == "thinking":
                        thinking_to_stream += item.get("thinking", "")
        if text_to_stream:
            self.ctx.data_engine.stream_output(stream_exec_id, text_to_stream, is_thinking=False)
        if thinking_to_stream:
            self.ctx.data_engine.stream_output(stream_exec_id, thinking_to_stream, is_thinking=True)

    async def _complete_attempt(self, emit_deltas: bool, *args, **kwargs):
        endpoint_key = self._endpoint_key()
        if endpoint_key in _NON_STREAMING_ENDPOINTS:
            return await self.base_model.ainvoke(*args, **kwargs)

        stream_exec_id = uuid4()
        full_response = None
        try:
            async for chunk in self.base_model.astream(*args, **kwargs):
                if full_response is None:
                    full_response = chunk
                else:
                    full_response = full_response + chunk
                if emit_deltas:
                    self._emit_stream_delta(stream_exec_id, chunk)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as stream_error:
            if full_response is not None:
                # Mid-stream failure: the partial output must never reach
                # message history. Tell the UI to discard it, then let the
                # recovery policy retry the whole call with a fresh stream id.
                llm_logger.warning(
                    f"LLM stream broke mid-response ({stream_error}); discarding partial output."
                )
                _record_llm_event(
                    "llm_stream_reset",
                    {
                        "stream_exec_id": str(stream_exec_id),
                        "action": "discard",
                        "reason": "mid_stream_failure",
                        "error": str(stream_error)[:500],
                        "category": classify_failure(stream_error).category.value,
                        "message": (
                            "A request error occurred during output generation, typically caused by lower API priority. Retrying automatically..."
                        ),
                    },
                )
                raise
            if _is_stream_unsupported_error(stream_error):
                _NON_STREAMING_ENDPOINTS.add(endpoint_key)
                llm_logger.warning(
                    f"Endpoint {endpoint_key} does not support streaming"
                    f" ({stream_error}); switching to non-streaming calls."
                )
                _record_llm_event(
                    "llm_stream_downgrade",
                    {"endpoint": endpoint_key, "error": str(stream_error)[:500]},
                )
                return await self.base_model.ainvoke(*args, **kwargs)
            raise
        if full_response is None:
            llm_logger.warning("LLM stream yielded no chunks; using non-streaming call.")
            return await self.base_model.ainvoke(*args, **kwargs)
        return full_response

    async def astream(self, *args, **kwargs):
        """Deprecated compatibility shim: yields exactly one final message.

        The old chunk-level astream retry could re-deliver already-yielded
        chunks after a mid-stream failure, corrupting accumulated message
        history. Streaming now happens inside complete(); live token deltas
        still reach the UI through the data engine.
        """
        yield await self.complete(*args, **kwargs)


def _is_stream_unsupported_error(error: BaseException) -> bool:
    """Detect 'this endpoint/model cannot stream' as opposed to a transient failure.

    A positive here permanently disables streaming for the endpoint in this
    process, so a bare AttributeError/TypeError from unrelated code must not
    qualify — the message has to actually implicate streaming.
    """
    if isinstance(error, NotImplementedError):
        return True
    message = str(error).lower()
    if isinstance(error, (AttributeError, TypeError)):
        return "stream" in message
    return "stream" in message and any(
        marker in message
        for marker in ("not support", "unsupported", "not implemented", "not available")
    )


async def acomplete(llm, *args, **kwargs):
    """Get one complete response from any chat model object.

    This is the single call-shape agents should use. For gateway-managed
    models (RobustChatModelWrapper) it delegates to complete(), which owns
    streaming, classified retries, and telemetry. For raw models and test
    doubles it preserves the legacy accumulate-stream-else-ainvoke shape.
    """
    if isinstance(llm, RobustChatModelWrapper):
        return await llm.complete(*args, **kwargs)

    try:
        full_response = None
        async for chunk in llm.astream(*args, **kwargs):
            if full_response is None:
                full_response = chunk
            else:
                full_response = full_response + chunk
        if full_response is not None:
            return full_response
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise
    except Exception as stream_error:
        llm_logger.warning(
            f"astream unavailable on {type(llm).__name__} ({stream_error}); using ainvoke."
        )
    return await llm.ainvoke(*args, **kwargs)


async def acomplete_structured(
    llm,
    messages: list,
    *,
    schema=None,
    correction_attempts: int = 1,
):
    """Complete and parse a JSON response, with one corrective re-ask.

    On a parse failure the model is shown its own output's parse error and
    asked to re-emit valid JSON (up to ``correction_attempts`` times) — giving
    the model a chance to see its mistake instead of feeding garbage
    downstream. If it still fails, raises StructuredOutputError (never
    returns raw text masquerading as parsed data). Each repair round emits an
    llm_parse_repair telemetry event.
    """
    response = await acomplete(llm, messages)
    text = content_to_text(getattr(response, "content", ""))
    parsed = parse_structured(text, schema=schema)
    attempt = 0
    while isinstance(parsed, ParseFailure) and attempt < correction_attempts:
        attempt += 1
        llm_logger.warning(
            f"Structured output parse failed ({parsed.error});"
            f" asking the model to correct itself (attempt {attempt})..."
        )
        _record_llm_event(
            "llm_parse_repair",
            {"error": parsed.error[:500], "attempt": attempt},
        )
        correction_messages = [
            *messages,
            response if isinstance(response, BaseMessage) else AIMessage(content=text),
            HumanMessage(
                content=(
                    "Your previous reply could not be parsed as the required"
                    f" JSON ({parsed.error}). Re-emit ONLY the corrected JSON"
                    " payload, with no surrounding prose or code fences."
                )
            ),
        ]
        response = await acomplete(llm, correction_messages)
        text = content_to_text(getattr(response, "content", ""))
        parsed = parse_structured(text, schema=schema)
    if isinstance(parsed, ParseFailure):
        _record_llm_event(
            "llm_gave_up",
            {
                "error": f"structured output unparseable: {parsed.error[:400]}",
                "category": "bad_request",
                "retryable": False,
            },
            status="failed",
        )
        raise StructuredOutputError(parsed)
    return parsed


async def invoke_llm_with_timeout_message[T](
    llm_call: Coroutine[Any, Any, T],
    timeout_seconds: int = 10,
    hard_timeout: int = 180,
) -> T:
    """Send an LLM call and display a countdown / timeout message if delayed."""
    llm_task = asyncio.create_task(llm_call)
    waiter_task = asyncio.create_task(asyncio.sleep(timeout_seconds))
    try:
        done, _ = await asyncio.wait({llm_task, waiter_task}, return_when=asyncio.FIRST_COMPLETED)

        if llm_task in done:
            return llm_task.result()

        user_messages_logger.info("Waiting for LLM call response...")
        start_time = asyncio.get_event_loop().time()

        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(llm_task), timeout=1.0)
            except TimeoutError:
                if llm_task.done():
                    return llm_task.result()

                pause_file = PAUSE_FILE
                if pause_file.exists():
                    start_time = asyncio.get_event_loop().time()
                    continue

                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > max(0, hard_timeout - timeout_seconds):
                    user_messages_logger.error(f"LLM call timed out after {hard_timeout} seconds.")
                    raise TimeoutError(f"LLM call timed out after {hard_timeout} seconds.")
    except BaseException:
        if not llm_task.done():
            llm_task.cancel()
        await asyncio.gather(llm_task, return_exceptions=True)
        raise
    finally:
        if not waiter_task.done():
            waiter_task.cancel()
        await asyncio.gather(waiter_task, return_exceptions=True)


# Backward compatible factory functions delegating to ModelFactory
def get_google_llm(
    model_name: str = "gemini-3.8-flash",
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
    model_name: str = "gemini-3.8-flash",
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
    """Cleanly resolves a ModelEndpoint from context llm_config."""
    if getattr(ctx, "llm_config", None) is None:
        try:
            ctx.llm_config = get_default_llm_config()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Config loading has open-ended failure modes; surface the real cause
            # instead of letting the attribute access below fail with a misleading
            # AttributeError on the still-missing config.
            raise RuntimeError(
                f"Default LLM config could not be loaded while resolving '{name}': {exc}"
            ) from exc

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
    model_val = getattr(cfg, "model", "gemini-2.5-flash")

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
        endpoint = endpoint.model_copy(update={"temperature": temperature})
    raw_model = ModelFactory.get_model(endpoint)

    handler = DataEngineCallbackHandler(ctx)
    bound_model = raw_model.with_config(callbacks=[handler])

    return RobustChatModelWrapper(bound_model, ctx, endpoint=endpoint)  # type: ignore


async def with_fallback[T](
    main_call: Callable[[], Awaitable[T]],
    fallback_call: Callable[[], Awaitable[T]],
    none_should_fallback: bool = True,
) -> T:
    """Run main_call, switching to fallback_call only when it can actually help.

    Falling back is an explicit, observed decision: the failure is classified
    and only categories where a different endpoint might succeed trigger the
    fallback (a bad request would just hide the bug inside a weaker model's
    output). Every switch is logged at WARNING and recorded as an
    llm_fallback telemetry event. While main_call runs, the recovery layer
    knows a fallback exists and hands over immediately on retry exhaustion
    instead of pausing the task.
    """

    def _switch(reason: str, category: str | None, error: str | None) -> None:
        llm_logger.warning(
            f"❗ Main LLM inference failed ({reason}"
            f"{f': {error}' if error else ''}). Falling back..."
        )
        _record_llm_event(
            "llm_fallback",
            {
                "reason": reason,
                "category": category,
                "error": error[:500] if error else None,
            },
        )

    # The contextvar must be reset BEFORE fallback_call runs: the fallback has
    # no further fallback behind it, so its own exhaustion should pause.
    fallback_token = _FALLBACK_AVAILABLE.set(True)
    try:
        result = await main_call()
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise
    except LLMCallError as e:
        _FALLBACK_AVAILABLE.reset(fallback_token)
        if not e.failure.should_fallback:
            raise
        _switch("terminal_error", e.failure.category.value, str(e))
        return await fallback_call()
    except Exception as e:
        _FALLBACK_AVAILABLE.reset(fallback_token)
        failure = classify_failure(e)
        if failure.category is FailureCategory.CANCELLED or not failure.should_fallback:
            raise
        _switch("error", failure.category.value, str(e))
        return await fallback_call()
    else:
        _FALLBACK_AVAILABLE.reset(fallback_token)

    if result is None and none_should_fallback:
        _switch("empty_result", None, None)
        return await fallback_call()
    return result
