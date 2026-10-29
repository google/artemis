# Copyright 2025-2026 Minitap, Inc.
# Modifications Copyright 2026 Google LLC
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
#
# Originally from mobile-use (https://github.com/minitap-ai/mobile-use).
# See third_party/mobile_use/METADATA for the upstream source and local modifications.

"""Single-task automation driver (upstream ``run_automation``).

Used by the ``artemis run`` CLI command in ``artemis.interfaces.cli.commands.run``.
"""

import os
from pathlib import Path
from typing import Annotated

import typer

from artemis import Agent, Builders
from artemis.config import initialize_llm_config, settings
from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder
from third_party.mobile_use.sdk.types.task import AgentProfile
from third_party.mobile_use.utils.video import check_ffmpeg_available

# CLI parameters of the ``run`` command.
GoalArgument = Annotated[str, typer.Argument(help="The main goal for the agent to achieve.")]
TestNameOption = Annotated[
    str | None,
    typer.Option(
        "--test-name",
        "-n",
        help="Name of the test run for trace recording.",
    ),
]
TracesPathOption = Annotated[
    str | None,
    typer.Option(
        "--traces-path",
        "-t",
        help="Directory where execution traces are persisted.",
    ),
]
OutputDescriptionOption = Annotated[
    str | None,
    typer.Option(
        "--output-description",
        "-o",
        help="Natural language or schema description of the expected output.",
    ),
]
VideoRecordingToolsOption = Annotated[
    bool | None,
    typer.Option(
        "--with-video-recording-tools/--without-video-recording-tools",
        help=(
            "Enable or disable dynamic video recording and screen analysis "
            "tools (auto-detected if omitted)."
        ),
    ),
]


def ensure_video_recording_available(with_video_recording_tools: bool | None) -> None:
    """Fail early when video recording tools are requested but ffmpeg is missing."""
    if with_video_recording_tools:
        check_ffmpeg_available()


def new_default_config_builder() -> AgentConfigBuilder:
    """Return a config builder set up with the default LLM profile and ADB server."""
    llm_config = initialize_llm_config()
    agent_profile = AgentProfile(name="default", llm_config=llm_config)
    config = Builders.AgentConfig.with_default_profile(profile=agent_profile)
    if settings.ADB_HOST:
        config.with_adb_server(host=settings.ADB_HOST, port=settings.ADB_PORT)
    return config


async def run_automation(
    config: AgentConfigBuilder,
    goal: str,
    session_id: str | None = None,
    locked_app_package: str | None = None,
    test_name: str | None = None,
    traces_output_path_str: str | None = None,
    output_description: str | None = None,
    profile: str | None = None,
    app_path: str | None = None,
) -> None:
    """Initialize an agent from ``config``, run ``goal`` as a single task, then clean up."""
    agent: Agent | None = None
    try:
        agent = Agent(config=config.build(), session_id=session_id)
        await agent.init(
            retry_count=int(os.getenv("ARTEMIS_HEALTH_RETRIES", 5)),
            retry_wait_seconds=int(os.getenv("ARTEMIS_HEALTH_DELAY", 2)),
        )

        task = agent.new_task(goal)
        if locked_app_package:
            task.with_locked_app_package(locked_app_package)
        if test_name:
            trace_path = traces_output_path_str or str(settings.TRACES_PATH)
            task.with_name(test_name).with_trace_recording(path=trace_path)
        if output_description:
            task.with_output_description(output_description)
        if profile:
            task.using_profile(profile)
        if app_path:
            task.with_app_path(Path(app_path))

        llm_result_path = os.getenv("RESULTS_OUTPUT_PATH", None)
        if llm_result_path:
            task.with_llm_output_saving(path=llm_result_path)

        await agent.run_task(request=task.build())
    finally:
        if agent is not None:
            await agent.clean()
