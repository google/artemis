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
    disable_checker: bool = False
    """Legacy master switch (compat alias): ``True`` disables BOTH the midway
    checkpoints and the final check, regardless of the individual gates below.
    Factory default is ``False``: the exit final review runs out of the box."""
    disable_midway_checks: bool = True
    """Midway checkpoints are OFF by default (factory layering: final check on,
    planner validation on, midway checks off)."""
    disable_final_check: bool = False
    checker_max_iterations: int = 20
    final_check_max_attempts: int = 3
    checkpoint_max_repairs: int = 2
    max_concurrent_checkpoints: int = 3
    checkpoint_timeout: float = 180.0
    settlement_timeout: float = 120.0
    assert_failure_policy: Literal["continue", "halt"] = "continue"
    disable_device_probes: bool = False
    disable_planner_validation: bool = False
    enable_committee: bool = False
    committee_debate_rounds: int = 2
    disable_outputter: bool = False
    outputter: OutputterConfig = Field(default_factory=OutputterConfig)
    explorer: ExplorerConfig = Field(default_factory=ExplorerConfig)
    explorer_versions: dict[str, str] = Field(default_factory=dict)

    @property
    def midway_checks_enabled(self) -> bool:
        """Midway checkpoint gate: honors both the individual switch and the
        legacy ``disable_checker`` master alias."""
        return not (self.disable_midway_checks or self.disable_checker)

    @property
    def final_check_enabled(self) -> bool:
        """Final review gate: honors both the individual switch and the legacy
        ``disable_checker`` master alias."""
        return not (self.disable_final_check or self.disable_checker)

    @property
    def checks_enabled(self) -> bool:
        """True when any check gate is active (drives conditional prompt assembly)."""
        return self.midway_checks_enabled or self.final_check_enabled

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
    def explorer_caching(self) -> bool | None:
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
    agent_config: Any = None
    adb_client: Any | None = None
    ui_adb_client: Any | None = None
    adb_task_registry: Any | None = None
    """Per-task ADB background-task registry (``artemis.tools.command_tool``).
    Created lazily on first ADB tool use; killed at task end."""

    execution_setup: ExecutionSetup | None = None

    data_engine: DataEngine | None = None
    planner_task: asyncio.Task[Any] | None = None
    background_tasks: list[asyncio.Task[Any]] = Field(default_factory=list)
    background_task_grace_period_seconds: float = 2.0
    package_cache: dict[str, str | None] = Field(default_factory=dict)
    background_jobs: dict[str, dict[str, Any]] = Field(default_factory=dict)

    pending_checkpoints: list[Any] = Field(default_factory=list)
    """Queued :class:`~artemis.graph.checkpoints.PendingCheckpoint` entries.
    ``_process_plan_write`` only enqueues; ``execution_check_node`` spawns after
    the turn's step is recorded so the evidence anchor points at a real step."""
    checkpoint_tasks: dict[str, Any] = Field(default_factory=dict)
    """checkpoint_id -> (attempt_id, asyncio.Task). One in-flight attempt per
    checkpoint; supersede harvests/cancels the old entry before replacing it."""
    checkpoint_attempt_seq: dict[str, int] = Field(default_factory=dict)
    """checkpoint_id -> monotonically increasing attempt counter."""
    checkpoint_repairs: dict[str, int] = Field(default_factory=dict)
    """checkpoint_id -> number of verify-fail repairs already applied."""
    assert_halt: bool = False
    """Latched by an assert failure under ``assert_failure_policy='halt'``."""
    final_check_attempts: int = 0
    """Number of final-check passes already executed at exit settlement."""

    last_validated_plan: str | None = None
    """Ratchet baseline for planner validation: the last plan content whose
    top-level milestones were validated (or the initial plan). Drift is always
    judged against this, never against the immediately preceding write."""
    pending_validated_plan: str | None = None
    """Plan content currently under async planner validation; becomes the new
    baseline when the validator approves it."""

    _genai_client: Any | None = PrivateAttr(default=None)
    _video_blackboard: Any | None = PrivateAttr(default=None)
    _video_circuit_breaker: Any | None = PrivateAttr(default=None)
    _mobile_controller: Any | None = PrivateAttr(default=None)
    _active_driver: Any | None = PrivateAttr(default=None)
    """Cached device driver singleton (artemis.drivers.factory.get_driver)."""
    mcp_client_ctx: Any | None = None
    mcp_session: Any | None = None
    action_session: Any | None = None
    """In-process unified action MCP session (artemis.mcp.action_session)."""
    actuator: Any | None = None
    """Optional actuator backend override (artemis.mcp.actuators). ``None`` selects
    the default AdbActuator; installing e.g. a robot-arm backend happens here."""

    step_memory: Any | None = None
    """Shared :class:`~artemis.memory.step_memory.StepMemoryService` composition
    root (history redesign §6.2). Created on first use via
    ``artemis.memory.ensure_step_memory``; Flash and Pro profiles share one
    instance so summary jobs, retries, and flush semantics are unified."""

    transcript_ledger: Any | None = None
    """Session :class:`~artemis.memory.transcript.TranscriptLedger` for the Pro
    operator's transcript prompt path (``agent.memory.transcript.enabled``).
    Lives beside ``step_memory``; the State never carries message payloads."""

    async def __aenter__(self) -> ArtemisContext:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Close the in-process action session before draining background tasks so its
        # owner task exits cleanly rather than being cancelled below.
        if self.action_session is not None:
            try:
                await self.action_session.aclose()
            except Exception as exc:
                logger.debug(f"Action session close failed; skipped: {exc}", exc_info=True)
            finally:
                self.action_session = None

        # Drain in-flight step-summary jobs before the generic background-task
        # sweep: the shared service owns its own bounded flush semantics. On an
        # exceptional exit the flush is skipped and the jobs are cancelled below.
        if self.step_memory is not None and callable(getattr(self.step_memory, "flush", None)):
            try:
                if exc_type is None:
                    await self.step_memory.flush()
                else:
                    await self.step_memory.flush(timeout_seconds=0.0)
            except Exception as exc:
                logger.debug(f"Step-memory flush failed; skipped: {exc}", exc_info=True)

        # Drain in-flight chunk capsule jobs the same way (M3): the chunk
        # manager persists any harvested capsules so the DB copy is complete
        # even though the frozen transcript text is not re-rendered.
        chunker = getattr(self.transcript_ledger, "chunker", None)
        if chunker is not None and callable(getattr(chunker, "flush", None)):
            try:
                if exc_type is None:
                    await chunker.flush()
                else:
                    await chunker.flush(timeout_seconds=0.0)
            except Exception as exc:
                logger.debug(f"History chunk flush failed; skipped: {exc}", exc_info=True)

        if self.background_tasks:
            tasks = list(self.background_tasks)
            pending_tasks = [task for task in tasks if not task.done()]
            grace_period = (
                max(0.0, self.background_task_grace_period_seconds) if exc_type is None else 0.0
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
            except Exception as exc:
                logger.debug(f"MCP client context exit failed; skipped: {exc}", exc_info=True)
            finally:
                self.mcp_client_ctx = None
                self.mcp_session = None

        if self.data_engine:
            try:
                await self.data_engine.shutdown()
            except Exception as exc:
                logger.debug(f"DataEngine shutdown failed; skipped: {exc}", exc_info=True)

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
