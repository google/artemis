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

"""Cross-platform Process Supervisor and Lifecycle Manager."""

import asyncio
from collections.abc import Sequence
import os
from pathlib import Path

from artemis.platform import platform
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class ProcessSupervisor:
    """Manages process spawning, tree cleanup, signals, and UTF-8 stream handling."""

    @staticmethod
    def prepare_environment(extra_env: dict[str, str] | None = None) -> dict[str, str]:
        """Prepare child process environment with guaranteed UTF-8 mode."""
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONUTF8"] = "1"
        if extra_env:
            env.update(extra_env)
        return env

    @staticmethod
    async def spawn_async(
        cmd: Sequence[str],
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
        capture_output: bool = True,
    ) -> asyncio.subprocess.Process:
        """Spawn an asynchronous subprocess with clean environment and cross-platform flags."""
        prepared_env = ProcessSupervisor.prepare_environment(env)
        cwd_str = str(cwd) if cwd else None

        stdout_dest = asyncio.subprocess.PIPE if capture_output else None
        stderr_dest = asyncio.subprocess.PIPE if capture_output else None

        return await asyncio.create_subprocess_exec(
            *cmd,
            stdout=stdout_dest,
            stderr=stderr_dest,
            cwd=cwd_str,
            env=prepared_env,
        )

    @staticmethod
    def terminate_tree(pid: int, timeout_seconds: float = 3.0) -> bool:
        """Recursively terminate a process tree."""
        return platform.process.terminate_process_tree(pid, timeout_seconds=timeout_seconds)

    @staticmethod
    async def stop_process(
        proc: asyncio.subprocess.Process | None,
        timeout_seconds: float = 3.0,
    ) -> None:
        """Safely stop an asyncio Process and kill its children if necessary."""
        if proc is None:
            return

        if proc.returncode is not None:
            return

        try:
            if proc.pid:
                ProcessSupervisor.terminate_tree(proc.pid, timeout_seconds=timeout_seconds)
            else:
                proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
        except Exception as e:
            logger.debug(f"Failed graceful stop for process {proc}: {e}")
            try:
                proc.kill()
            except Exception:
                pass


process_supervisor: ProcessSupervisor = ProcessSupervisor()
