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

import asyncio
import inspect
import os
import re

import uuid
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from langchain_google_genai import ChatGoogleGenerativeAI

from artemis.clients.screen_client_factory import (
    describe_backend,
    helper_version,
    hierarchy_backend_sentence,
)
from artemis.config import (
    CheckerConfig,
    run_tuning_for_profile,
    settings,
)
from artemis.context import (
    ArtemisContext,
    DevicePlatform,
    ExecutionSetup,
)
from artemis.data_engine.engine import DataEngine
from artemis.graph.state import State
from artemis.runtime import trace_store
from artemis.runtime.cancel_requests import watch_for_cancel_request
from artemis.sdk.run_outcome import attach_test_summary, resolve_trace_suffix
from artemis.sdk.types.agent import AgentConfig
from artemis.utils.startup_progress import publish_startup_progress
from third_party.mobile_use.sdk.types.exceptions import AgentError
from third_party.mobile_use.sdk.types.task import Task
from third_party.mobile_use.sdk.agent import AgentBase, TOutput
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

load_dotenv()


__all__ = ["Agent", "TOutput", "attach_test_summary", "resolve_trace_suffix", "run_tuning_summary"]


def run_tuning_summary(config: AgentConfig, profile: str | None) -> dict[str, str] | None:
    """Per-run Pro tuning (verification level, explorer mode) for an SDK config.

    The SDK config carries the Checker switches as flat flags; fold them back
    into a :class:`CheckerConfig` so the ladder classification stays single-
    sourced in ``artemis.config``. Returns ``None`` for Flash runs.
    """
    return run_tuning_for_profile(
        profile,
        checker=CheckerConfig(
            enabled=not config.disable_checker,
            midway_checks=not config.disable_midway_checks,
            final_check=not config.disable_final_check,
            assert_failure_policy=config.assert_failure_policy,
        ),
        explorer=config.explorer,
        explorer_versions=dict(config.explorer_versions or {}),
    )


class Agent(AgentBase):
    """Artemis SDK agent."""

    def __init__(
        self,
        *,
        config: AgentConfig | None = None,
        device_id: str | None = None,
        device_serial: str | None = None,
        concurrency_mode: str | None = None,
        max_concurrency: int | None = None,
        session_id: str | None = None,
    ):
        raw_sid = (
            session_id or os.getenv("ARTEMIS_SESSION_ID") or os.getenv("ARTEMIS_CLOUD_SESSION_ID")
        )
        self._session_id: str | None = str(raw_sid).strip() if raw_sid else None
        target_dev = device_serial or device_id
        if config is None:
            from artemis.sdk.builders import Builders

            builder = Builders.AgentConfig
            if target_dev:
                builder.for_device(DevicePlatform.ANDROID, target_dev)
            if concurrency_mode:
                builder.with_concurrency_mode(concurrency_mode)
            if max_concurrency is not None:
                builder.with_max_concurrency(max_concurrency)
            self._config = builder.build()
        else:
            self._config = config
            updates = {}
            if target_dev:
                updates["device_id"] = target_dev
                updates["device_platform"] = DevicePlatform.ANDROID
            if concurrency_mode:
                updates["concurrency_mode"] = str(concurrency_mode).strip().lower()
            if max_concurrency is not None:
                updates["max_concurrency"] = max_concurrency
            if updates:
                self._config = self._config.model_copy(update=updates)

        self._tasks = []
        self._tmp_traces_dir = Path(settings.TRACES_PATH)
        self._initialized = False
        self._task_lock = asyncio.Lock()

    async def _prewarm_llm_connections(self, api_key: str | None = None):
        """Pre-warms the HTTP2/gRPC connection pools for both Native GenAI and LangChain clients in the background."""
        if os.environ.get("ARTEMIS_FAKE_LLM") == "1":
            logger.info("ARTEMIS_FAKE_LLM=1 — skipping real LLM connection pre-warming.")
            publish_startup_progress(
                "model_ready", "Model connection is ready (fake LLM)", session_id=self._session_id
            )
            return
        publish_startup_progress(
            "model_warmup", "Warming the model connection", session_id=self._session_id
        )
        logger.info("Starting background pre-warming of Gemini API connection pools...")
        try:
            key = api_key
            if not key and settings.GOOGLE_API_KEY:
                key = settings.GOOGLE_API_KEY.get_secret_value()

            if not key:
                logger.warning("Skipping LLM pre-warming: No API key available.")
                publish_startup_progress(
                    "model_ready",
                    "Model connection will initialize on first use",
                    session_id=self._session_id,
                )
                return

            warmup_model = "gemini-3.8-flash"
            is_google_provider = True
            if hasattr(self, "_context") and self._context and hasattr(self._context, "llm_config"):
                default_cfg = getattr(self._context.llm_config, "default", None)
                if default_cfg:
                    provider = getattr(default_cfg, "provider", "google")
                    if isinstance(provider, str) and provider.lower() not in ("google", "gemini"):
                        is_google_provider = False
                    if getattr(default_cfg, "model", None):
                        warmup_model = default_cfg.model

            if not is_google_provider:
                logger.info("Skipping Gemini pre-warming: non-Google provider configured.")
                publish_startup_progress(
                    "model_ready", "Model connection is ready", session_id=self._session_id
                )
                return

            # 1. Pre-warm Native SDK client
            client = genai.Client(api_key=key)

            # 2. Pre-warm LangChain client
            chat = ChatGoogleGenerativeAI(model=warmup_model, google_api_key=key)

            # Fire both calls concurrently in the background
            await asyncio.gather(
                client.aio.models.count_tokens(model=warmup_model, contents="ping"),
                chat.ainvoke("ping"),
                return_exceptions=True,
            )
            logger.success("Gemini API connection pools successfully pre-warmed.")
            publish_startup_progress(
                "model_ready", "Model connection is ready", session_id=self._session_id
            )
        except Exception as e:
            logger.warning(f"Failed to pre-warm LLM connections: {e}")
            publish_startup_progress(
                "model_ready",
                "Model connection will initialize on first use",
                session_id=self._session_id,
            )

    async def _watch_external_cancel(self, task_name: str) -> None:
        """Cancel the running task when another process drops a cancel marker.

        The admin console (and other ingresses) cannot deliver a signal to a
        worker portably, so they write a marker keyed by session id / pid.
        Reacting here routes the request through the same cancellation path
        as Ctrl+C: the recording is stopped and remuxed, the trace folder is
        compiled and renamed, and the device lease is released.
        """

        def _on_cancel() -> None:
            logger.warning(
                f"[{task_name}] External cancel request received; stopping the task gracefully."
            )
            self.stop_current_task()

        try:
            await watch_for_cancel_request(_on_cancel, session_id=self._session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"[{task_name}] Cancel watcher stopped: {exc}")

    async def clean(self, force: bool = False):
        if not self._initialized and not force:
            return

        if self._ui_adb_client is not None:
            await asyncio.to_thread(self._ui_adb_client.disconnect)
        self._initialized = False
        logger.info("✅ Artemis agent stopped.")

    async def _ensure_device_unlocked(self) -> None:
        """Reject secure keyguard instead of allowing an agent to guess credentials."""
        if self._adb_client is None:
            raise AgentError("ADB client is not initialized.")

        device = self._adb_client.device(serial=self._device_context.device_id)
        try:
            trust_state = str(await asyncio.to_thread(device.shell, "dumpsys trust"))
        except Exception as exc:
            logger.warning(f"Could not inspect Android keyguard state: {exc}")
            return

        # The first deviceLocked value belongs to the current Android user;
        # later entries may describe a separately locked work profile.
        match = re.search(r"\bdeviceLocked=(?:true|1|false|0)\b", trust_state, re.IGNORECASE)
        if match is None:
            return
        value = match.group(0).split("=", 1)[1].lower()
        if value in {"true", "1"}:
            raise AgentError(
                "Android secure keyguard is locked. Unlock the device manually before "
                "running Artemis; automation will not guess a PIN, password, or pattern."
            )

    async def _prepare_device_environment(self, context: ArtemisContext):
        """Prepare device environment flags (like forcing Web Accessibility) before the task runs."""
        if not self._config.force_web_accessibility:
            logger.info(
                "Forcing web accessibility is disabled in AgentConfig. Skipping"
                " device environment prep..."
            )
            return

        if not self._adb_client or not self._device_context:
            logger.warning(
                "ADB client or device context not available. Skipping device environment prep..."
            )
            return

        device_id = self._device_context.device_id
        try:
            device = self._adb_client.device(serial=device_id)
            logger.info(f"[{device_id}] ⚙️ Configuring Chrome/WebView Web Accessibility flags...")

            # 1. Write the force accessibility command flag to Chrome command line
            await asyncio.to_thread(
                device.shell,
                "echo 'chrome --force-renderer-accessibility' >"
                " /data/local/tmp/chrome-command-line",
            )
            await asyncio.to_thread(device.shell, "chmod 555 /data/local/tmp/chrome-command-line")

            # 2. Write the same command line flag to System WebView config file
            await asyncio.to_thread(
                device.shell,
                "echo 'chrome --force-renderer-accessibility' >"
                " /data/local/tmp/webview-command-line",
            )
            await asyncio.to_thread(device.shell, "chmod 555 /data/local/tmp/webview-command-line")

            # 3. Force stop Chrome so the flag takes effect next time it launches
            await asyncio.to_thread(device.shell, "am force-stop com.android.chrome")
            logger.success(
                f"[{device_id}] ✅ Chrome Web Accessibility command line flags"
                " configured successfully."
            )
        except Exception as e:
            logger.warning(
                "Failed to configure Chrome Web Accessibility flags (needs"
                f" root/userdebug device): {e}"
            )

    def _prepare_tracing(self, task: Task, context: ArtemisContext):
        """Prepare tracing and data engine setup."""
        task_name = self._prepare_trace_paths(task)

        context.execution_setup = ExecutionSetup(
            traces_path=self._tmp_traces_dir,
            trace_name=task_name,
            enable_remote_tracing=task.request.enable_remote_tracing
            if task.request.record_trace
            else False,
            video_recording_tools_enabled=self._config.video_recording_tools_enabled,
            disable_checker=self._config.disable_checker,
            disable_midway_checks=self._config.disable_midway_checks,
            disable_final_check=self._config.disable_final_check,
            checker_max_iterations=self._config.checker_max_iterations,
            final_check_max_attempts=self._config.final_check_max_attempts,
            checkpoint_max_repairs=self._config.checkpoint_max_repairs,
            max_concurrent_checkpoints=self._config.max_concurrent_checkpoints,
            checkpoint_timeout=self._config.checkpoint_timeout,
            settlement_timeout=self._config.settlement_timeout,
            assert_failure_policy=self._config.assert_failure_policy,
            disable_device_probes=self._config.disable_device_probes,
            disable_planner_validation=self._config.disable_planner_validation,
            enable_committee=self._config.enable_committee,
            committee_debate_rounds=self._config.committee_debate_rounds,
            disable_outputter=self._config.disable_outputter,
            outputter=self._config.outputter,
            explorer=self._config.explorer,
            explorer_versions=self._config.explorer_versions,
        )

        context.data_engine = DataEngine(ctx=context)
        device_data = context.device.model_dump() if context.device else {}
        if task.request.profile:
            device_data["profile"] = task.request.profile
        run_tuning = run_tuning_summary(self._config, task.request.profile)
        if run_tuning:
            device_data["run_tuning"] = run_tuning

        target_sid = (
            self._session_id
            or os.getenv("ARTEMIS_SESSION_ID")
            or os.getenv("ARTEMIS_CLOUD_SESSION_ID")
            or getattr(task, "id", None)
            or getattr(getattr(task, "request", None), "task_name", None)
        )
        sess_uuid = None
        if target_sid:
            try:
                sess_uuid = uuid.UUID(str(target_sid))
            except Exception:
                sess_uuid = str(target_sid)

        context.data_engine.start_session(
            goal=task.request.goal,
            device_info=device_data,
            session_id=sess_uuid,
        )

    async def _finalize_tracing_safely(self, task: Task, context: ArtemisContext):
        """Finalize optional trace artifacts without changing task semantics."""
        try:
            await self._finalize_tracing(task=task, context=context)
        except Exception as exc:
            logger.error(
                f"[{task.get_name()}] Trace artifact finalization failed after task status "
                f"was resolved as '{task.status}': {exc}",
                exc_info=True,
            )

    def _get_graph_state(self, task: Task):
        return State.initial(task.request.goal)

    async def _connect_screen_client(self, context: ArtemisContext, session_id: str) -> None:
        """Connect the screen client with visible progress for slow first-time steps."""
        client = self._ui_adb_client
        previous_listener = getattr(self, "_hierarchy_backend_listener", None)
        if previous_listener is not None:
            previous_client, listener = previous_listener
            previous_client.remove_backend_listener(listener)
            self._hierarchy_backend_listener = None
        publish_startup_progress(
            "uiautomator", "Connecting to the UI hierarchy service", session_id=session_id
        )

        def on_provision(event: str, details: dict) -> None:
            version = details.get("version_name") or details.get("to_version")
            if event == "installing":
                publish_startup_progress(
                    "helper_install",
                    "Installing the Artemis accessibility helper on this device for the "
                    f"first time (v{version}, about 3 seconds)",
                    session_id=session_id,
                    **details,
                )
            elif event == "upgrading":
                publish_startup_progress(
                    "helper_upgrade",
                    "Upgrading the Artemis accessibility helper "
                    f"(v{details.get('from_version')} -> v{details.get('to_version')})",
                    session_id=session_id,
                    **details,
                )

        # Clients without provisioning (UIAutomator2, cloud) take no callback.
        try:
            accepts_events = "on_event" in inspect.signature(client.connect).parameters
        except (TypeError, ValueError):
            accepts_events = False
        if accepts_events:
            await asyncio.to_thread(client.connect, on_event=on_provision)
        else:
            await asyncio.to_thread(client.connect)

        backend = describe_backend(client) or "uiautomator"
        publish_startup_progress(
            "uiautomator_ready",
            f"UI hierarchy service is ready ({backend})",
            session_id=session_id,
        )
        self._announce_hierarchy_backend(context, session_id, None, backend, None)
        add_listener = getattr(client, "add_backend_listener", None)
        if callable(add_listener):

            def listener(previous, new, reason):
                self._announce_hierarchy_backend(context, session_id, previous, new, reason)

            add_listener(listener)
            self._hierarchy_backend_listener = (client, listener)

    def _announce_hierarchy_backend(
        self,
        context: ArtemisContext,
        session_id: str,
        previous: str | None,
        backend: str,
        reason: str | None,
    ) -> None:
        """One line everywhere a person or an agent looks for it.

        Startup-progress event (UI timeline), a named log trace, the session's
        device_info, and status.json for ``mobile_manage_task``. Runs from
        worker threads too, so every sink is best effort.
        """
        label = {"helper": "Artemis accessibility helper", "uiautomator": "UIAutomator2"}
        version = helper_version(self._ui_adb_client)
        if previous is None:
            message = f"UI hierarchy source: {label.get(backend, backend)}"
            if backend == "helper" and version:
                message += f" v{version}"
            stage = "hierarchy_backend"
        else:
            message = (
                f"UI hierarchy source switched from {label.get(previous, previous)} to "
                f"{label.get(backend, backend)}"
            )
            if reason:
                message += f" because {reason}"
            stage = "hierarchy_backend_changed"
        logger.info(message)
        try:
            publish_startup_progress(
                stage,
                message,
                session_id=session_id,
                backend=backend,
                previous_backend=previous,
                reason=reason,
                helper_version=version,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            logger.debug(f"Could not publish hierarchy backend progress: {exc}")
        engine = getattr(context, "data_engine", None)
        if engine is not None and getattr(engine, "current_session_id", None):
            try:
                engine.record_trace(
                    type="log",
                    name="hierarchy_backend",
                    payload={
                        "message": message,
                        "backend": backend,
                        "previous_backend": previous,
                        "reason": reason,
                        "helper_version": version,
                        "level": "WARNING" if previous is not None else "INFO",
                    },
                )
                engine.update_session_device_info(
                    hierarchy_backend=backend,
                    hierarchy_backend_note=hierarchy_backend_sentence(
                        self._ui_adb_client, relative_time=engine.get_relative_time
                    ),
                )
            except (OSError, ValueError, RuntimeError) as exc:
                logger.debug(f"Could not record hierarchy backend in the data engine: {exc}")
        try:
            trace_store.update_trace_fields(
                session_id,
                hierarchy_backend=backend,
                hierarchy_backend_note=hierarchy_backend_sentence(self._ui_adb_client),
            )
        except (OSError, ValueError, RuntimeError) as exc:
            logger.debug(f"Could not record hierarchy backend in status.json: {exc}")
