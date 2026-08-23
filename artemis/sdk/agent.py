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
import os

try:
    from datetime import UTC, datetime
except ImportError:
    from datetime import datetime

    UTC = UTC
from pathlib import Path
from platform import system
import shutil
from shutil import which
import sys
from types import NoneType
from typing import TypeVar, overload
import uuid

from adbutils import AdbClient
from dotenv import load_dotenv
from google import genai
from langchain_core.messages import AIMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from PIL import Image
from pydantic import BaseModel

from artemis.agents.flash.runner import FlashRunner
from artemis.agents.outputter.outputter import outputter
from artemis.clients.ui_automator_client import UIAutomatorClient
from artemis.config import OutputConfig, record_events, settings
from artemis.constants import RECURSION_LIMIT
from artemis.context import (
    ArtemisContext,
    DeviceContext,
    DevicePlatform,
    ExecutionSetup,
)
from artemis.controllers.controller_factory import get_controller
from artemis.controllers.platform_specific_commands_controller import (
    get_first_device,
)
from artemis.runtime import DeviceExecutionLock
from artemis.data_engine.engine import DataEngine
from artemis.data_engine.trace import DataEngineCallbackHandler
from artemis.graph.graph import get_graph
from artemis.graph.state import State
from artemis.sdk.builders.agent_config_builder import get_default_agent_config
from artemis.sdk.builders.task_request_builder import TaskRequestBuilder
from artemis.sdk.types.agent import AgentConfig
from artemis.sdk.types.exceptions import (
    AgentError,
    AgentNotInitializedError,
    AgentProfileNotFoundError,
    AgentTaskRequestError,
    DeviceNotFoundError,
    ExecutableNotFoundError,
)
from artemis.sdk.types.task import (
    AgentProfile,
    Task,
    TaskRequest,
)
from artemis.utils.app_launch_utils import _handle_initial_app_launch
from artemis.utils.logger import get_logger
from artemis.utils.media import (
    create_gif_from_trace_folder,
    create_steps_json_from_trace_folder,
    remove_images_from_trace_folder,
    remove_steps_json_from_trace_folder,
)

logger = get_logger(__name__)

TOutput = TypeVar("TOutput", bound=BaseModel | None)

load_dotenv()


class Agent:
    _config: AgentConfig
    _tasks: list[Task] = []
    _tmp_traces_dir: Path
    _initialized: bool = False
    _device_context: DeviceContext
    _adb_client: AdbClient | None
    _ui_adb_client: UIAutomatorClient | None

    _current_task: asyncio.Task | None = None
    _task_lock: asyncio.Lock
    _cloud_mobile_id: str | None = None

    def __init__(self, *, config: AgentConfig | None = None):
        self._config = config or get_default_agent_config()
        self._tasks = []
        self._tmp_traces_dir = Path(settings.TRACES_PATH)
        self._initialized = False
        self._task_lock = asyncio.Lock()

    async def init(
        self,
        api_key: str | None = None,
        retry_count: int = 5,
        retry_wait_seconds: int = 5,
    ):

        return await self._init_internal(
            api_key=api_key,
            retry_count=retry_count,
            retry_wait_seconds=retry_wait_seconds,
        )

    async def _init_internal(
        self,
        api_key: str | None = None,
        retry_count: int = 5,
        retry_wait_seconds: int = 5,
    ):

        if os.environ.get("ARTEMIS_CLOUD_MODE") != "1" and not which("adb"):
            raise ExecutableNotFoundError("adb")

        if self._initialized:
            logger.warning("Agent is already initialized. Skipping...")
            return True

        # Get first available device ID
        if os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
            device_id = os.environ.get("ADB_DEVICE_SERIAL", "cloud_device")
            platform = DevicePlatform.ANDROID
        elif not self._config.device_id or not self._config.device_platform:
            device_id, platform, _ = get_first_device(logger=logger)
        else:
            device_id, platform = (
                self._config.device_id,
                self._config.device_platform,
            )

        if not device_id or not platform:
            error_msg = "No device found. Exiting."
            logger.error(error_msg)
            raise DeviceNotFoundError(error_msg)

        # Initialize clients
        if os.environ.get("ARTEMIS_CLOUD_MODE") != "1":
            self._init_clients(
                device_id=device_id,
                platform=platform,
            )
        else:
            from cloud_service.virtualization import RemoteAdbClient, RemoteUIAutomatorClient

            self._adb_client = RemoteAdbClient()
            self._ui_adb_client = RemoteUIAutomatorClient(
                device_id=device_id,
                adb_client=self._adb_client,
            )

        self._device_context = await self._get_device_context(
            device_id=device_id, platform=platform
        )
        logger.info(self._device_context.to_str())

        # Asynchronously pre-warm LLM connection pools in the background
        asyncio.create_task(self._prewarm_llm_connections(api_key))

        logger.info("✅ Artemis agent initialized.")
        self._initialized = True

        return True

    async def _prewarm_llm_connections(self, api_key: str | None = None):
        """Pre-warms the HTTP2/gRPC connection pools for both Native GenAI and LangChain clients in the background."""
        logger.info("Starting background pre-warming of Gemini API connection pools...")
        try:
            key = api_key
            if not key and settings.GOOGLE_API_KEY:
                key = settings.GOOGLE_API_KEY.get_secret_value()

            if not key:
                logger.warning("Skipping LLM pre-warming: No API key available.")
                return

            # 1. Pre-warm Native SDK client
            client = genai.Client(api_key=key)

            # 2. Pre-warm LangChain client
            chat = ChatGoogleGenerativeAI(model="gemini-3.7-flash", google_api_key=key)

            # Fire both calls concurrently in the background
            await asyncio.gather(
                client.aio.models.count_tokens(model="gemini-3.7-flash", contents="ping"),
                chat.ainvoke("ping"),
                return_exceptions=True,
            )
            logger.success("Gemini API connection pools successfully pre-warmed.")
        except Exception as e:
            logger.warning(f"Failed to pre-warm LLM connections: {e}")

    async def install_apk(self, apk_path: str | Path) -> None:
        """Install an APK on the connected device.

        For cloud mobiles, the APK must be x86_64 compatible.

        Args:
            apk_path: Path to the local APK file to install

        Raises:
            AgentNotInitializedError: If the agent is not initialized
            AgentError: If attempting to install on non-Android device or ADB
            operations fail
            FileNotFoundError: If the APK file doesn't exist
            CloudMobileServiceUninitializedError: If cloud service is
            unavailable
        """
        await self._install_apk_internal(apk_path)

    async def _install_apk_internal(self, apk_path: str | Path) -> None:
        if isinstance(apk_path, str):
            apk_path = Path(apk_path)

        if not apk_path.exists():
            raise FileNotFoundError(f"APK file not found: {apk_path}")

        if not self._initialized:
            raise AgentNotInitializedError()

        device_id = self._device_context.device_id
        logger.info(f"Installing APK on Android device '{device_id}'")
        if not self._adb_client:
            raise AgentError("ADB client not initialized")

        device = self._adb_client.device(serial=device_id)
        await asyncio.to_thread(device.install, apk_path)
        logger.info(f"APK installed successfully on Android device '{device_id}'")

    async def install_app(self, app_path: str | Path) -> str | None:
        """Install an app on the connected device.

        For Android: Installs an APK file using ADB.

        Args:
            app_path: Path to the app to install (Android APK file).

        Returns:
            None.

        Raises:
            AgentNotInitializedError: If the agent is not initialized
            AgentError: If installation fails or platform is unsupported
            FileNotFoundError: If the app file/folder doesn't exist
        """
        return await self._install_app_internal(app_path)

    async def _install_app_internal(self, app_path: str | Path) -> str | None:
        if isinstance(app_path, str):
            app_path = Path(app_path)

        if not app_path.exists():
            raise FileNotFoundError(f"App not found: {app_path}")

        if not self._initialized:
            raise AgentNotInitializedError()

        await self._install_apk_internal(app_path)
        return None

    def new_task(self, goal: str):
        """Create a new task request builder.

        Args:
            goal: Natural language description of what to accomplish

        Returns:
            TaskRequestBuilder that can be configured with:
            - .with_output_format() for structured output
            - .with_output_description() for output description
            - .with_locked_app_package() to restrict execution to a specific app
            - .using_profile() to specify an LLM profile
            - .with_max_steps() to set maximum execution steps
            - .with_trace_recording() to enable trace recording
            - .with_name() to set a custom task name
        """
        return TaskRequestBuilder[None].from_common(
            goal=goal,
            common=self._config.task_request_defaults,
        )

    @overload
    async def run_task(
        self,
        *,
        goal: str,
        output: type[TOutput],
        profile: str | AgentProfile | None = None,
        name: str | None = None,
        locked_app_package: str | None = None,
        app_path: str | Path | None = None,
    ) -> TOutput | None: ...

    @overload
    async def run_task(
        self,
        *,
        goal: str,
        output: str,
        profile: str | AgentProfile | None = None,
        name: str | None = None,
        locked_app_package: str | None = None,
        app_path: str | Path | None = None,
    ) -> str | dict | None: ...

    @overload
    async def run_task(
        self,
        *,
        goal: str,
        output=None,
        profile: str | AgentProfile | None = None,
        name: str | None = None,
        locked_app_package: str | None = None,
        app_path: str | Path | None = None,
    ) -> str | None: ...

    @overload
    async def run_task(
        self,
        *,
        request: TaskRequest[None],
        locked_app_package: str | None = None,
        app_path: str | Path | None = None,
    ) -> str | dict | None: ...

    @overload
    async def run_task(
        self,
        *,
        request: TaskRequest[TOutput],
        locked_app_package: str | None = None,
        app_path: str | Path | None = None,
    ) -> TOutput | None: ...

    async def run_task(
        self,
        *,
        goal: str | None = None,
        output: type[TOutput] | str | None = None,
        profile: str | AgentProfile | None = None,
        locked_app_package: str | None = None,
        name: str | None = None,
        app_path: str | Path | None = None,
        request: TaskRequest[TOutput] | None = None,
    ) -> str | dict | TOutput | None:

        # Normal local execution path
        if request is not None:
            if locked_app_package is not None:
                if request.locked_app_package:
                    logger.warning(
                        "Locked app package specified both in the request and"
                        " as a parameter. Using the parameter value."
                    )
                request.locked_app_package = locked_app_package
            # Handle app_path parameter override
            if app_path is not None:
                if request.app_path:
                    logger.warning(
                        "App path specified both in the request and as a"
                        " parameter. Using the parameter value."
                    )
                request.app_path = Path(app_path) if isinstance(app_path, str) else app_path
            return await self._run_task(request=request)
        if goal is None:
            raise AgentTaskRequestError("Goal is required")
        task_request = self.new_task(goal=goal)
        if output is not None:
            if isinstance(output, str):
                task_request.with_output_description(description=output)
            elif output is not NoneType:
                task_request.with_output_format(output_format=output)
        if profile is not None:
            task_request.using_profile(profile=profile)
        if name is not None:
            task_request.with_name(name=name)
        if locked_app_package is not None:
            task_request.with_locked_app_package(package_name=locked_app_package)
        if app_path is not None:
            task_request.with_app_path(app_path=app_path)
        return await self._run_task(task_request.build())

    async def _run_task(
        self,
        request: TaskRequest[TOutput],
    ) -> str | dict | TOutput | None:
        if not self._initialized:
            raise AgentNotInitializedError()

        if request.profile:
            agent_profile = self._config.agent_profiles.get(request.profile)
            if agent_profile is None:
                if request.profile.lower() in (
                    "flash",
                    "pro",
                    "ultra",
                    "default",
                ):
                    agent_profile = self._config.default_profile
                else:
                    raise AgentProfileNotFoundError(request.profile)
        else:
            agent_profile = self._config.default_profile

        logger.info(str(agent_profile))

        on_status_changed = None
        task_id = str(uuid.uuid4())

        task = Task(
            id=task_id,
            device=self._device_context,
            status="pending",
            request=request,
            created_at=datetime.now(),
            on_status_changed=on_status_changed,
        )
        self._tasks.append(task)
        task_name = task.get_name()

        context = ArtemisContext(
            device=self._device_context,
            adb_client=self._adb_client,
            ui_adb_client=self._ui_adb_client,
            llm_config=agent_profile.llm_config,
            agent_config=self._config,
        )

        self._prepare_tracing(task=task, context=context)
        await self._prepare_app_installation(task=task)
        await self._prepare_device_environment(context=context)
        await self._prepare_app_lock(task=task, context=context)
        self._prepare_output_files(task=task)

        output_config = None
        if request.output_description or request.output_format:
            output_config = OutputConfig(
                output_description=request.output_description,
                structured_output=request.output_format,  # type: ignore
            )
            logger.info(str(output_config))

        logger.info(f"[{task_name}] Starting graph with goal: `{request.goal}`")
        state = self._get_graph_state(task=task)
        graph_input = state.model_dump()
        datetime.now(UTC)

        async def _execute_task_logic():
            last_state: State | None = None
            last_state_snapshot: dict | None = None
            output = None
            device_lock = DeviceExecutionLock(
                self._device_context.device_id,
                description=f"automation task: {request.goal[:120]}",
            )
            try:
                if os.environ.get("ARTEMIS_CLOUD_MODE") != "1":
                    await asyncio.to_thread(device_lock.acquire)
                async with context:
                    recording_started = False
                    if self._config.video_recording_tools_enabled:
                        try:
                            controller = get_controller(context)

                            output_dir = None
                            if context.execution_setup and context.execution_setup.traces_path:
                                output_dir = (
                                    self._tmp_traces_dir / context.execution_setup.trace_name
                                ).resolve()

                            logger.info(f"[{task_name}] Starting automated screen recording...")
                            start_res = await controller.start_video_recording(
                                output_dir=output_dir
                            )
                            if start_res and start_res.success:
                                recording_started = True
                            else:
                                logger.warning(
                                    f"[{task_name}] Failed to start screen"
                                    f" recording: {start_res.message if start_res else 'unknown'}"
                                )
                        except Exception as e:
                            logger.error(f"[{task_name}] Failed to start screen recording: {e}")

                    try:
                        if request.profile and request.profile.lower() == "flash":
                            logger.info(f"[{task_name}] Invoking FlashRunner reactive loop...")
                            await task.set_status(
                                status="running",
                                message="Invoking FlashRunner...",
                            )

                            max_turns = (
                                request.max_steps if request.max_steps != RECURSION_LIMIT else 35
                            )
                            runner = FlashRunner(context, goal=request.goal, max_turns=max_turns)
                            flash_result = await runner.run(state)

                            output = flash_result
                            last_state_snapshot = state.model_dump()

                            status = flash_result.get("status")
                            if status == "completed":
                                logger.info(f"✅ Automation '{task_name}' is success ✅")
                                await task.finalize(content=output, state=last_state_snapshot)
                                if context.data_engine:
                                    context.data_engine.end_session("success")
                            else:
                                err = (
                                    f"[{task_name}] FlashRunner failed:"
                                    f" {flash_result.get('explanation')}"
                                )
                                logger.warning(err)
                                await task.finalize(
                                    content=output,
                                    state=last_state_snapshot,
                                    error=err,
                                )
                                if context.data_engine:
                                    context.data_engine.end_session("failed")
                            return output
                        else:
                            logger.info(f"[{task_name}] Invoking graph with input: {graph_input}")
                            await task.set_status(status="running", message="Invoking graph...")
                            async for chunk in (await get_graph(context)).astream(
                                input=graph_input,
                                config={
                                    "recursion_limit": task.request.max_steps,
                                    "callbacks": (
                                        (
                                            list(self._config.graph_config_callbacks)
                                            if self._config.graph_config_callbacks is not None
                                            else []
                                        )
                                        + [DataEngineCallbackHandler(context)]
                                        if context.data_engine
                                        else self._config.graph_config_callbacks
                                    ),
                                },
                                stream_mode=[
                                    "messages",
                                    "custom",
                                    "updates",
                                    "values",
                                ],
                            ):
                                stream_mode, payload = chunk
                                if stream_mode == "values":
                                    last_state_snapshot = payload  # type: ignore
                                    last_state = State(**last_state_snapshot)  # type: ignore

                            if not last_state:
                                err = f"[{task_name}] No result received from graph"
                                logger.warning(err)
                                await task.finalize(
                                    content=output,
                                    state=last_state_snapshot,
                                    error=err,
                                )
                                if context.data_engine:
                                    context.data_engine.end_session("failed")
                                return None

                            print_ai_response_to_stderr(graph_result=last_state)
                            output = await self._extract_output(
                                task_name=task_name,
                                ctx=context,
                                request=request,
                                output_config=output_config,
                                state=last_state,
                            )
                            logger.info(f"✅ Automation '{task_name}' is success ✅")
                            await task.finalize(content=output, state=last_state_snapshot)
                            if context.data_engine:
                                context.data_engine.end_session("success")

                            return output
                    finally:
                        if recording_started:
                            try:
                                logger.info(f"[{task_name}] Stopping automated screen recording...")

                                controller = get_controller(context)
                                stop_result = await controller.stop_video_recording()
                                if stop_result.success and stop_result.video_path:
                                    logger.info(
                                        f"[{task_name}] Screen recording saved"
                                        f" to: {stop_result.video_path}"
                                    )
                                else:
                                    logger.warning(
                                        f"[{task_name}] Failed to save screen"
                                        f" recording: {stop_result.message}"
                                    )
                            except Exception as e:
                                logger.error(f"[{task_name}] Error stopping screen recording: {e}")
            except asyncio.CancelledError:
                err = f"[{task_name}] Task cancelled"
                logger.warning(err)
                await task.finalize(
                    content=output,
                    state=last_state_snapshot,
                    error=err,
                    cancelled=True,
                )
                if context.data_engine:
                    context.data_engine.end_session("cancelled")

                raise
            except Exception as e:
                err = f"[{task_name}] Error running automation: {e}"
                logger.error(err)
                await task.finalize(
                    content=output,
                    state=last_state_snapshot,
                    error=err,
                )
                if context.data_engine:
                    context.data_engine.end_session("failed")

                raise
            finally:
                try:
                    await self._finalize_tracing_safely(task=task, context=context)
                finally:
                    if os.environ.get("ARTEMIS_CLOUD_MODE") != "1":
                        await asyncio.to_thread(device_lock.release)

        async with self._task_lock:
            if self._current_task and not self._current_task.done():
                logger.warning(
                    "Another automation task is already running. "
                    "Stopping it before starting the new one."
                )
                self.stop_current_task()
                try:
                    await self._current_task
                except asyncio.CancelledError:
                    pass

            try:
                self._current_task = asyncio.create_task(_execute_task_logic())
                return await self._current_task
            finally:
                self._current_task = None

    def stop_current_task(self):
        """Requests cancellation of the currently running automation task."""
        if self._current_task and not self._current_task.done():
            logger.info("Requesting to stop the current automation task...")
            was_cancelled = self._current_task.cancel()
            if was_cancelled:
                logger.success("Cancellation request for the current task was sent.")
            else:
                logger.warning(
                    "Could not send cancellation request for the current task "
                    "(it may already be completing)."
                )
        else:
            logger.info("No active automation task to stop.")

    async def get_screenshot(self) -> Image.Image:
        """Capture a screenshot from the mobile device.

        Returns:
            Screenshot as PIL Image

        Raises:
            AgentNotInitializedError: If the agent is not initialized
            Exception: If screenshot capture fails
        """
        if not self._initialized:
            raise AgentNotInitializedError()

        # Use ADB to capture screenshot
        logger.info("Capturing screenshot from local Android device")
        if not self._adb_client:
            raise Exception("ADB client not initialized")

        device = self._adb_client.device(serial=self._device_context.device_id)
        screenshot = await asyncio.to_thread(device.screenshot)
        logger.info("Screenshot captured from local Android device")
        return screenshot

    async def clean(self, force: bool = False):
        if not self._initialized and not force:
            return

        self._initialized = False
        logger.info("✅ Artemis agent stopped.")

    async def _prepare_app_installation(self, task: Task) -> str | None:
        """Install app if app_path is specified in the task request.

        Returns:
            None.
        """
        if not task.request.app_path:
            return None

        task_name = task.get_name()
        logger.info(f"[{task_name}] Installing app from: {task.request.app_path}")

        await self.install_app(task.request.app_path)

        return None

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

    async def _prepare_app_lock(self, task: Task, context: ArtemisContext):
        """Prepare app lock by launching the locked app if specified."""
        if not task.request.locked_app_package:
            return

        task_name = task.get_name()
        logger.info(f"[{task_name}] Preparing app lock for: {task.request.locked_app_package}")

        app_lock_status = await _handle_initial_app_launch(
            ctx=context, locked_app_package=task.request.locked_app_package
        )

        if context.execution_setup is None:
            context.execution_setup = ExecutionSetup(app_lock_status=app_lock_status)
        else:
            context.execution_setup.app_lock_status = app_lock_status

        if app_lock_status.locked_app_initial_launch_success is False:
            error = app_lock_status.locked_app_initial_launch_error
            logger.warning(f"[{task_name}] Failed to launch locked app: {error}")

    def _prepare_tracing(self, task: Task, context: ArtemisContext):
        """Prepare tracing and data engine setup."""
        task_name = task.get_name()
        temp_trace_path = Path(self._tmp_traces_dir / task_name).resolve()
        temp_trace_path.mkdir(parents=True, exist_ok=True)

        if task.request.record_trace:
            traces_output_path = Path(task.request.trace_path).resolve()
            logger.info(f"[{task_name}] 📂 Traces output path: {traces_output_path}")
            logger.info(f"[{task_name}] 📄📂 Traces temp path: {temp_trace_path}")
            traces_output_path.mkdir(parents=True, exist_ok=True)

        context.execution_setup = ExecutionSetup(
            traces_path=self._tmp_traces_dir,
            trace_name=task_name,
            enable_remote_tracing=task.request.enable_remote_tracing
            if task.request.record_trace
            else False,
            video_recording_tools_enabled=self._config.video_recording_tools_enabled,
            disable_checker=self._config.disable_checker,
            checker_max_iterations=self._config.checker_max_iterations,
            checker_max_chat_rounds=self._config.checker_max_chat_rounds,
            disable_planner_validation=self._config.disable_planner_validation,
            planner_validation_threshold=self._config.planner_validation_threshold,
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
        context.data_engine.start_session(goal=task.request.goal, device_info=device_data)

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

    async def _finalize_tracing(self, task: Task, context: ArtemisContext):
        if context.data_engine:
            session_status = "completed"
            if task.status == "failed":
                session_status = "failed"
            elif task.status == "cancelled":
                session_status = "cancelled"
            context.data_engine.end_session(status=session_status)

        exec_setup_ctx = context.execution_setup
        if not exec_setup_ctx:
            return

        if exec_setup_ctx.traces_path is None or exec_setup_ctx.trace_name is None:
            return

        task_name = task.get_name()
        temp_trace_path = (self._tmp_traces_dir / exec_setup_ctx.trace_name).resolve()

        if not task.request.record_trace:
            try:
                if temp_trace_path.exists():
                    shutil.rmtree(temp_trace_path)
                    logger.info(f"[{task_name}] Cleaned up temporary trace folder.")
            except Exception as e:
                logger.error(f"[{task_name}] Failed to clean up temp trace folder: {e}")
            return

        status = "_PASS" if task.status == "completed" else "_FAIL"
        ts = task.created_at.strftime("%Y-%m-%dT%H-%M-%S")
        new_name = f"{exec_setup_ctx.trace_name}{status}_{ts}"

        traces_output_path = Path(task.request.trace_path).resolve()

        logger.info(f"[{task_name}] Compiling trace FROM FOLDER: " + str(temp_trace_path))
        create_gif_from_trace_folder(temp_trace_path)
        create_steps_json_from_trace_folder(temp_trace_path)

        logger.info(f"[{task_name}] Video created, removing dust...")
        remove_images_from_trace_folder(temp_trace_path)
        remove_steps_json_from_trace_folder(temp_trace_path)
        logger.info(f"[{task_name}] 📽️ Trace compiled, moving to output path 📽️")

        output_folder_path = temp_trace_path.rename(traces_output_path / new_name).resolve()
        logger.info(f"[{task_name}] 📂✅ Traces located in: {output_folder_path}")

        new_video_path = output_folder_path / "recording.mp4"
        if new_video_path.exists() and context.data_engine:
            context.data_engine.update_video_path(new_video_path)

    def _prepare_output_files(self, task: Task):
        if task.request.llm_output_path:
            _validate_and_prepare_file(file_path=task.request.llm_output_path)

    async def _extract_output(
        self,
        task_name: str,
        ctx: ArtemisContext,
        request: TaskRequest[TOutput],
        output_config: OutputConfig | None,
        state: State,
    ) -> str | dict | TOutput | None:
        exec_setup = getattr(ctx, "execution_setup", None)
        outputter_cfg = getattr(exec_setup, "outputter", None) or (
            getattr(ctx.agent_config, "outputter", None) if ctx and ctx.agent_config else None
        )
        outputter_enabled = getattr(outputter_cfg, "enabled", True) if outputter_cfg else True
        force_synthesis = (
            getattr(outputter_cfg, "force_synthesis", False) if outputter_cfg else False
        )
        if exec_setup and getattr(exec_setup, "disable_outputter", False):
            outputter_enabled = False

        should_run = outputter_enabled and (
            (
                output_config
                and output_config.needs_structured_format(default_enabled=outputter_enabled)
            )
            or force_synthesis
            or (output_config and output_config.enable_outputter is True)
        )
        if output_config and output_config.enable_outputter is False:
            should_run = False

        if should_run:
            logger.info(f"[{task_name}] Generating structured output via Outputter...")
            try:
                structured_output = await outputter(
                    ctx=ctx,
                    output_config=output_config or OutputConfig(),
                    graph_output=state,
                )
                logger.info(f"[{task_name}] Structured output: {structured_output}")
                record_events(
                    output_path=request.llm_output_path,
                    events=structured_output,
                )
                if request.output_format is not None and request.output_format is not NoneType:
                    return request.output_format.model_validate(structured_output)
                return structured_output
            except Exception as e:
                logger.error(f"[{task_name}] Failed to generate structured output: {e}")
                return None
        if state and state.messages:
            for msg in reversed(state.messages):
                if msg.content:
                    logger.info(f"Fallback output from message: {msg.content}")
                    record_events(output_path=request.llm_output_path, events=msg.content)
                    return msg.content
        return None

    def _get_graph_state(self, task: Task):
        return State(
            messages=[],
            initial_goal=task.request.goal,
            subgoal_plan=[],
            latest_ui_hierarchy=None,
            latest_screenshot=None,
            focused_app_info=None,
            device_date=None,
            structured_decisions=None,
            complete_subgoals_by_ids=[],
            remaining_steps=task.request.max_steps,
            validator_messages=[],
        )

    def _init_clients(
        self,
        device_id: str,
        platform: DevicePlatform,
    ):
        self._adb_client = AdbClient(
            host=self._config.servers.adb_host,
            port=self._config.servers.adb_port,
        )
        self._ui_adb_client = UIAutomatorClient(device_id=device_id)

    async def _get_device_context(
        self,
        device_id: str,
        platform: DevicePlatform,
    ) -> DeviceContext:

        host_platform = system()
        if os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
            width, height = (1080, 2400)
            if self._adb_client:
                try:
                    width, height = self._adb_client.device(device_id).window_size()
                    logger.info(
                        f"Retrieved remote device screen dimensions dynamically: {width}x{height}"
                    )
                except Exception as e:
                    logger.debug(
                        f"Failed to query remote window size: {e}. Defaulting to 1080x2400."
                    )
            return DeviceContext(
                host_platform="LINUX",
                mobile_platform=platform,
                device_id=device_id,
                device_width=width,
                device_height=height,
            )

        # Get real device dimensions from the device
        if self._ui_adb_client:
            try:
                # Use UIAutomator to get actual screen dimensions
                screen_data = self._ui_adb_client.get_screen_data()
                device_width = screen_data.width
                device_height = screen_data.height
                logger.info(f"Retrieved Android screen dimensions: {device_width}x{device_height}")
            except Exception as e:
                logger.warning(f"Failed to get Android screen dimensions: {e}, using defaults")
                device_width, device_height = 1080, 2340
        else:
            logger.warning("UIAutomator client not available, using default dimensions")
            device_width, device_height = 1080, 2340

        from artemis.platform import platform as pal_platform

        return DeviceContext(
            host_platform=pal_platform.os_type.name,
            mobile_platform=platform,
            device_id=device_id,
            device_width=device_width,
            device_height=device_height,
        )


def _validate_and_prepare_file(file_path: Path):
    path_obj = Path(file_path)
    if path_obj.exists() and path_obj.is_dir():
        raise AgentTaskRequestError(f"Error: Path '{file_path}' is a directory, not a file.")
    try:
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        path_obj.touch(exist_ok=True)
    except OSError as e:
        raise AgentTaskRequestError(f"Error creating file '{file_path}': {e}")


def print_ai_response_to_stderr(graph_result: State):
    for msg in reversed(graph_result.messages):
        if isinstance(msg, AIMessage):
            print(msg.content, file=sys.stderr)
            return
