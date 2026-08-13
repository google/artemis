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

"""Benchmark and evaluation command (artemis bench)."""

import asyncio
from pathlib import Path
import subprocess
import sys
from typing import Annotated

from artemis.config import initialize_llm_config
from artemis.sdk import Agent
from artemis.sdk.builders import Builders
from artemis.sdk.types.task import AgentProfile
from artemis.utils.logger import get_logger
import typer

logger = get_logger(__name__)


def check_and_start_emulator(adb_path: str = "adb") -> bool:
    """Checks if Android emulator is running; optionally starts it."""
    try:
        res = subprocess.run([adb_path, "devices"], capture_output=True, text=True, timeout=5)
        for line in res.stdout.splitlines():
            if "emulator-" in line and "device" in line:
                return True
    except Exception:
        pass
    return False


async def run_benchmark(
    task_name: str | None = None,
    profile_name: str = "pro",
    console_port: int = 5554,
    adb_path: str = "adb",
) -> None:
    """Runs AndroidWorld benchmark task evaluations."""
    # Add android_world-main if present
    workspace_root = Path(__file__).resolve().parent.parent.parent.parent.parent
    aw_path = workspace_root / "android_world-main"
    if aw_path.exists() and str(aw_path) not in sys.path:
        sys.path.insert(0, str(aw_path))

    try:
        from android_world import registry
        from android_world.env import env_launcher
    except ImportError:
        logger.error("AndroidWorld benchmark suite is not installed or available in path.")
        raise typer.Exit(1)

    task_registry = registry.TaskRegistry()
    aw_registry = task_registry.get_registry(task_registry.ANDROID_WORLD_FAMILY)

    if task_name and task_name not in aw_registry:
        logger.error(f"Task '{task_name}' not found in AndroidWorld registry.")
        available = sorted(list(aw_registry.keys()))
        logger.info(f"Available tasks ({len(available)}): {', '.join(available[:10])}...")
        raise typer.Exit(1)

    logger.info(f"Initializing AndroidWorld environment on console port {console_port}...")
    env = env_launcher.load_and_setup_env(
        console_port=console_port,
        emulator_setup=False,
        adb_path=adb_path,
    )

    llm_config = initialize_llm_config()
    profile = AgentProfile(name="default", llm_config=llm_config)
    config = Builders.AgentConfig.with_default_profile(profile).build()

    agent = Agent(config=config)
    await agent.init()

    tasks_to_run = [task_name] if task_name else list(aw_registry.keys())

    try:
        for t_name in tasks_to_run:
            task_type = aw_registry[t_name]
            params = task_type.generate_random_params()
            task_instance = task_type(params)

            logger.info(f"\n🎯 [Benchmark] Starting: {t_name} | Goal: {task_instance.goal}")
            task_instance.initialize_task(env)

            result = await agent.run_task(
                goal=task_instance.goal,
                name=f"bench_{t_name}",
                profile=profile_name,
            )
            success = task_instance.is_successful(env)
            logger.info(f"🏆 Task '{t_name}' Result: {result} | Benchmark Verified: {success}")
    finally:
        await agent.clean()
        env.close()


def bench_command(
    task: Annotated[
        str | None,
        typer.Option(
            "--task",
            "-t",
            help="Specific AndroidWorld task name to evaluate (e.g. Clock_1, Calculator_1).",
        ),
    ] = None,
    profile: Annotated[
        str,
        typer.Option(
            "--profile",
            "-p",
            help="Agent profile to evaluate ('flash' or 'pro').",
        ),
    ] = "pro",
    adb_path: Annotated[
        str,
        typer.Option(
            "--adb-path",
            help="Path to ADB executable.",
        ),
    ] = "adb",
    console_port: Annotated[
        int,
        typer.Option(
            "--port",
            help="Emulator console port.",
        ),
    ] = 5554,
) -> None:
    """Run AndroidWorld benchmark task evaluations against the agent."""
    if not check_and_start_emulator(adb_path=adb_path):
        typer.secho(
            f"Warning: No active emulator detected on port {console_port}. Please start an emulator.",
            fg=typer.colors.YELLOW,
        )

    asyncio.run(
        run_benchmark(
            task_name=task,
            profile_name=profile,
            console_port=console_port,
            adb_path=adb_path,
        )
    )
