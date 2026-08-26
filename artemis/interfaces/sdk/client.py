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
import uuid
from typing import Any, Literal
from collections.abc import AsyncGenerator, Callable
from artemis.engine.pipeline import Pipeline
from artemis.interfaces.sdk.task import StreamEvent, StreamEventType, Task, TaskResult
from artemis.runtime import (
    ConcurrencyMode,
    DeviceBusyError,
    DeviceExecutionLock,
    DeviceStatus,
    device_pool,
    ensure_daemon_running,
)
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class ArtemisClient:
    """The official high-level developer entrypoint for interacting with ARTEMIS."""

    def __init__(
        self,
        device_id: str | None = None,
        device_serial: str | None = None,
        default_profile: Literal["flash", "pro"] = "pro",
        concurrency_mode: Literal["global", "per_device"] | str = "per_device",
        max_concurrency: int | None = None,
        standalone: bool = False,
    ):
        self.standalone = standalone or os.environ.get("ARTEMIS_STANDALONE", "").lower() in ("1", "true", "yes")
        if not self.standalone:
            try:
                ensure_daemon_running(timeout=1.5)
            except Exception:
                pass

        target_dev = device_serial or device_id
        if target_dev is None or target_dev == "default-device":
            self._device_id = device_pool.select_device() or "default-device"
        else:
            self._device_id = target_dev

        self.default_profile = default_profile
        self._concurrency_mode = str(concurrency_mode).strip().lower()
        self._max_concurrency = max_concurrency

    @property
    def device_id(self) -> str:
        return self._device_id

    @device_id.setter
    def device_id(self, val: str) -> None:
        self._device_id = val

    @property
    def device_serial(self) -> str:
        """Alias for device_id."""
        return self._device_id

    @device_serial.setter
    def device_serial(self, val: str) -> None:
        self._device_id = val

    def set_device(self, device_serial: str) -> "ArtemisClient":
        """Set target device serial and return client for chaining."""
        self._device_id = device_serial
        return self

    @property
    def concurrency_mode(self) -> str:
        """Concurrency mode: 'global' (1 across all devices) or 'per_device' (1 per device)."""
        return self._concurrency_mode

    @concurrency_mode.setter
    def concurrency_mode(self, mode: Literal["global", "per_device"] | str) -> None:
        self._concurrency_mode = str(mode).strip().lower()

    @property
    def max_concurrency(self) -> int | None:
        return self._max_concurrency

    @max_concurrency.setter
    def max_concurrency(self, val: int | None) -> None:
        self._max_concurrency = val

    def set_concurrency_mode(self, mode: Literal["global", "per_device"] | str) -> "ArtemisClient":
        """Set concurrency mode ('global' or 'per_device') and return client for chaining."""
        self.concurrency_mode = mode
        return self

    def list_devices(self) -> list[DeviceStatus]:
        """List all connected Android devices with their lock state and availability."""
        return device_pool.list_devices()

    async def list_devices_async(self) -> list[DeviceStatus]:
        """Asynchronously list all connected Android devices with their lock state."""
        return await device_pool.list_devices_async()

    def get_idle_devices(self) -> list[DeviceStatus]:
        """Return connected devices currently ready and idle (not holding a task lock)."""
        return device_pool.get_idle_devices()

    async def run(
        self,
        goal: str,
        profile: Literal["flash", "pro"] | None = None,
        device_id: str | None = None,
        device_serial: str | None = None,
        concurrency_mode: Literal["global", "per_device"] | str | None = None,
        max_concurrency: int | None = None,
        blocking: bool = True,
        lock_timeout: float | None = None,
        max_turns: int = 30,
        **kwargs: Any,
    ) -> TaskResult:
        """Executes a single autonomous task on a target device and returns the final TaskResult."""
        target_profile = profile or self.default_profile
        target_device = device_serial or device_id or self.device_id
        if target_device == "default-device":
            target_device = device_pool.select_device() or "default-device"

        mode = concurrency_mode or self.concurrency_mode
        concurrency_limit = max_concurrency if max_concurrency is not None else self.max_concurrency

        logger.info(
            f"ArtemisClient running task '{goal}' on device '{target_device}' "
            f"with profile '{target_profile}' (concurrency_mode='{mode}')..."
        )

        task_session_id = str(uuid.uuid4())
        lock = DeviceExecutionLock(
            device_id=target_device,
            description=f"{goal[:120]}",
            concurrency_mode=mode,
            max_concurrency=concurrency_limit,
            session_id=task_session_id,
            ingress="sdk",
        )

        await asyncio.to_thread(
            lock.acquire,
            blocking=blocking,
            timeout=lock_timeout,
        )

        try:
            # Check if targeting a live connected device
            devices = device_pool.list_devices()
            connected_serials = {d.serial for d in devices}
            is_live_device = target_device in connected_serials and os.environ.get("ARTEMIS_MOCK_DRIVER") != "1"

            if is_live_device:
                from artemis.sdk.agent import Agent
                from artemis.sdk.builders import Builders
                from artemis.context import DevicePlatform

                cfg_builder = (
                    Builders.AgentConfig
                    .for_device(DevicePlatform.ANDROID, target_device)
                    .with_concurrency_mode(mode)
                )
                if concurrency_limit is not None:
                    cfg_builder.with_max_concurrency(concurrency_limit)

                agent = Agent(config=cfg_builder.build())
                agent._session_id = task_session_id
                await agent.init()
                try:
                    task = agent.new_task(goal=goal)
                    if target_profile:
                        task.using_profile(target_profile)
                    if kwargs.get("locked_package"):
                        task.with_locked_app_package(kwargs["locked_package"])

                    output = await agent.run_task(request=task.build())
                    return TaskResult(
                        trace_id=task_session_id,
                        status="completed" if output is not None else "success",
                        turns=getattr(agent, "_turns_completed", 1),
                        output=output,
                        device_id=target_device,
                    )
                finally:
                    await agent.clean()

            state = await Pipeline.execute(
                goal=goal,
                profile=target_profile,
                device_id=target_device,
                **kwargs,
            )

            return TaskResult(
                trace_id=state.trace_id,
                status=state.status.value,
                turns=state.current_turn,
                error=state.error_message,
                device_id=target_device,
            )
        finally:
            await asyncio.to_thread(lock.release)

    async def stream_run(
        self,
        goal: str,
        profile: Literal["flash", "pro"] | None = None,
        device_id: str | None = None,
        device_serial: str | None = None,
        concurrency_mode: Literal["global", "per_device"] | str | None = None,
        max_concurrency: int | None = None,
        blocking: bool = True,
        lock_timeout: float | None = None,
        max_turns: int = 30,
        callbacks: list[Callable[[StreamEvent], Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Executes an autonomous task while yielding real-time StreamEvents asynchronously."""
        target_profile = profile or self.default_profile
        target_device = device_serial or device_id or self.device_id
        if target_device == "default-device":
            target_device = device_pool.select_device() or "default-device"

        mode = concurrency_mode or self.concurrency_mode
        concurrency_limit = max_concurrency if max_concurrency is not None else self.max_concurrency

        logger.info(
            f"ArtemisClient streaming task '{goal}' on device '{target_device}' "
            f"with profile '{target_profile}' (concurrency_mode='{mode}')..."
        )

        lock = DeviceExecutionLock(
            device_id=target_device,
            description=f"ArtemisClient streaming task: {goal[:120]}",
            concurrency_mode=mode,
            max_concurrency=concurrency_limit,
        )

        await asyncio.to_thread(
            lock.acquire,
            blocking=blocking,
            timeout=lock_timeout,
        )
        try:
            # 1. Emit initial starting event
            start_event = StreamEvent(
                event_type=StreamEventType.STATUS,
                step_number=0,
                payload={
                    "status": "starting",
                    "goal": goal,
                    "profile": target_profile,
                    "device_id": target_device,
                    "concurrency_mode": mode,
                },
            )
            if callbacks:
                for cb in callbacks:
                    try:
                        cb(start_event)
                    except Exception as e:
                        logger.debug(f"Callback error: {e}")
            yield start_event

            # 2. Execute task pipeline
            try:
                state = await Pipeline.execute(
                    goal=goal,
                    profile=target_profile,
                    device_id=target_device,
                    **kwargs,
                )

                # 3. Emit intermediate step events
                for step in state.steps:
                    ev = StreamEvent(
                        event_type=StreamEventType.STEP_END,
                        step_number=step.step_number,
                        payload={
                            "thought": step.thought,
                            "action": step.action_name,
                            "params": step.action_params,
                            "result": step.result,
                            "duration": step.duration_seconds,
                        },
                    )
                    if callbacks:
                        for cb in callbacks:
                            try:
                                cb(ev)
                            except Exception as e:
                                logger.debug(f"Callback error: {e}")
                    yield ev

                # 4. Emit completion event
                final_ev = StreamEvent(
                    event_type=StreamEventType.STATUS,
                    step_number=state.current_turn,
                    payload={
                        "status": state.status.value,
                        "turns": state.current_turn,
                        "error": state.error_message,
                        "device_id": target_device,
                    },
                )
                if callbacks:
                    for cb in callbacks:
                        try:
                            cb(final_ev)
                        except Exception as e:
                            logger.debug(f"Callback error: {e}")
                yield final_ev

            except Exception as e:
                err_ev = StreamEvent(
                    event_type=StreamEventType.ERROR,
                    step_number=0,
                    payload={"error": str(e), "device_id": target_device},
                )
                if callbacks:
                    for cb in callbacks:
                        try:
                            cb(err_ev)
                        except Exception:
                            pass
                yield err_ev
        finally:
            await asyncio.to_thread(lock.release)

    async def run_task(
        self,
        task: Task,
        profile: Literal["flash", "pro"] | None = None,
        device_id: str | None = None,
        device_serial: str | None = None,
        concurrency_mode: Literal["global", "per_device"] | str | None = None,
        **kwargs: Any,
    ) -> TaskResult:
        """Executes a Task instance using ArtemisClient."""
        target_device = (
            device_serial
            or device_id
            or task.device_serial
            or task.device_id
            or self.device_id
        )
        target_profile = profile or task.profile or self.default_profile
        target_mode = concurrency_mode or task.concurrency_mode or self.concurrency_mode
        return await self.run(
            goal=task.goal,
            profile=target_profile,
            device_id=target_device,
            concurrency_mode=target_mode,
            max_turns=task.max_turns,
            locked_package=task.locked_package,
            **kwargs,
        )
