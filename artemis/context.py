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

"""Context variables for global state management.

Uses ContextVar to avoid prop drilling and maintain clean function signatures.
"""

from __future__ import annotations

import asyncio

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass


from pathlib import Path
from typing import Any, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from artemis.data_engine.engine import DataEngine

try:
    from adbutils import AdbClient
except ImportError:
    AdbClient = Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from artemis.utils.video import detect_video_tools_enabled


from artemis.config import ExplorerConfig, LLMConfig, OutputterConfig


from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class AppLaunchResult(BaseModel):
    """Result of initial app launch attempt."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    locked_app_package: str
    locked_app_initial_launch_success: bool | None
    locked_app_initial_launch_error: str | None


class DevicePlatform(StrEnum):
    """Mobile device platform enumeration."""

    ANDROID = "android"


class DeviceContext(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="allow",
    )

    host_platform: Literal["WINDOWS", "LINUX", "DARWIN", "MACOS"] | str = "DARWIN"
    mobile_platform: DevicePlatform = DevicePlatform.ANDROID
    device_id: str = "default-device"

    device_width: int = 1080
    device_height: int = 2400

    def to_str(self):
        return (
            f"Host platform: {self.host_platform}\n"
            f"Mobile platform: {self.mobile_platform.value}\n"
            f"Device ID: {self.device_id}\n"
            f"Device width: {self.device_width}\n"
            f"Device height: {self.device_height}\n"
        )


class ExecutionSetup(BaseModel):
    """Execution setup for a task."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="allow",
    )

    traces_path: Path | None = None
    trace_name: str | None = None
    enable_remote_tracing: bool = False
    app_lock_status: AppLaunchResult | None = None
    video_recording_tools_enabled: bool = Field(default_factory=detect_video_tools_enabled)
    disable_checker: bool = True
    checker_max_iterations: int = 20
    checker_max_chat_rounds: int = 4
    disable_planner_validation: bool = True
    planner_validation_threshold: float = 0.85
    enable_committee: bool = False
    committee_debate_rounds: int = 2
    disable_outputter: bool = False
    outputter: OutputterConfig = Field(default_factory=OutputterConfig)
    explorer: ExplorerConfig = Field(default_factory=ExplorerConfig)
    explorer_versions: dict[str, str] = Field(default_factory=dict)

    @property
    def explorer_version(self) -> str:
        return self.explorer.default_version

    @property
    def explorer_flash_mode(self) -> str:
        return self.explorer.flash_mode

    @property
    def explorer_pro_mode(self) -> str:
        return self.explorer.pro_mode

    @property
    def explorer_caching(self) -> bool:
        return self.explorer.caching

    def get_locked_app_package(self) -> str | None:
        """Get the locked app package name if app locking is enabled.

        Returns:
            The locked app package name, or None if app locking is not enabled.
        """
        if self.app_lock_status:
            return self.app_lock_status.locked_app_package
        return None


class ArtemisContext(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="allow",
    )

    device: DeviceContext
    llm_config: LLMConfig | None = None
    model_router: Any | None = None
    agent_config: Any = None
    adb_client: Any | None = None
    ui_adb_client: Any | None = None

    execution_setup: ExecutionSetup | None = None

    data_engine: DataEngine | None = None
    checker_task: asyncio.Task[Any] | None = None
    planner_task: asyncio.Task[Any] | None = None
    background_tasks: list[asyncio.Task[Any]] = Field(default_factory=list)
    background_task_grace_period_seconds: float = 2.0
    package_cache: dict[str, str | None] = Field(default_factory=dict)
    background_jobs: dict[str, dict[str, Any]] = Field(default_factory=dict)

    task_plan_snapshot: Path | None = None
    task_plan_content_before: str | None = None

    _genai_client: Any | None = PrivateAttr(default=None)
    _video_blackboard: Any | None = PrivateAttr(default=None)
    _video_circuit_breaker: Any | None = PrivateAttr(default=None)
    _mobile_controller: Any | None = PrivateAttr(default=None)
    mcp_client_ctx: Any | None = None
    mcp_session: Any | None = None

    async def __aenter__(self) -> ArtemisContext:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.background_tasks:
            tasks = list(self.background_tasks)
            pending_tasks = [task for task in tasks if not task.done()]
            grace_period = (
                max(0.0, self.background_task_grace_period_seconds)
                if exc_type is None
                else 0.0
            )

            if pending_tasks and grace_period > 0:
                logger.info(
                    f"Draining {len(pending_tasks)} background tasks for up to "
                    f"{grace_period:.1f}s..."
                )
                _, pending_tasks = await asyncio.wait(
                    pending_tasks,
                    timeout=grace_period,
                )

            if pending_tasks:
                logger.info(
                    f"Cancelling {len(pending_tasks)} unfinished background tasks; "
                    "primary automation is already complete."
                )
                for task in pending_tasks:
                    task.cancel()

            results = await asyncio.gather(*tasks, return_exceptions=True)
            for task, result in zip(tasks, results):
                if isinstance(result, Exception):
                    logger.error(
                        f"Background task {task.get_name()} failed: {result}",
                        exc_info=result,
                    )
            self.background_tasks.clear()

        if self.mcp_client_ctx:
            try:
                await self.mcp_client_ctx.__aexit__(exc_type, exc_val, exc_tb)
            except Exception:
                pass
            finally:
                self.mcp_client_ctx = None
                self.mcp_session = None

        if self.data_engine:
            try:
                await self.data_engine.shutdown()
            except Exception:
                pass

    def get_adb_client(self) -> Any:
        if self.adb_client is None:
            raise ValueError("No ADB client in context.")
        return self.adb_client

    def get_ui_adb_client(self) -> Any:
        if self.ui_adb_client is None:
            raise ValueError("No UIAutomator client in context.")
        return self.ui_adb_client


from artemis.data_engine.engine import DataEngine

ArtemisContext.model_rebuild()
