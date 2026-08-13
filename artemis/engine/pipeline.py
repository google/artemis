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

from typing import Literal

from artemis.config import OutputConfig, load_agent_config
from artemis.core.context import ExecutionContext
from artemis.core.state import ExecutionContextState
from artemis.drivers.base import BaseDeviceDriver
from artemis.drivers.mock.mock_driver import MockDeviceDriver
from artemis.engine.base_runner import BaseRunner
from artemis.engine.graph_runner import GraphRunner
from artemis.engine.reactive_runner import ReactiveRunner
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class Pipeline:
    """Orchestrates runner selection and execution flow."""

    @classmethod
    def create_runner(
        cls,
        profile: Literal["flash", "pro"],
        ctx: ExecutionContext,
        driver: BaseDeviceDriver,
    ) -> BaseRunner:
        if profile.lower() == "flash":
            return ReactiveRunner(ctx=ctx, driver=driver)
        return GraphRunner(ctx=ctx, driver=driver)

    @classmethod
    async def execute(
        cls,
        goal: str,
        profile: Literal["flash", "pro"] = "pro",
        driver: BaseDeviceDriver | None = None,
        device_id: str = "default-device",
        output_config: OutputConfig | None = None,
        enable_outputter: bool | None = None,
    ) -> ExecutionContextState:
        ctx = ExecutionContext(task_goal=goal, device_id=device_id)
        if not driver:
            driver = MockDeviceDriver(device_id=device_id)

        runner = cls.create_runner(profile=profile, ctx=ctx, driver=driver)
        state = await runner.run()

        # Check Outputter configuration
        try:
            agent_cfg = load_agent_config()
            outputter_cfg = getattr(agent_cfg, "outputter", None)
            is_enabled = (
                enable_outputter
                if enable_outputter is not None
                else (outputter_cfg.enabled if outputter_cfg else True)
            )
            force_synth = outputter_cfg.force_synthesis if outputter_cfg else False

            should_synthesize = is_enabled and (
                (
                    output_config
                    and output_config.needs_structured_format(default_enabled=is_enabled)
                )
                or force_synth
            )

            if should_synthesize:
                logger.info("Executing post-execution Outputter synthesis in Pipeline...")
                summary = "\n".join(
                    f"- Step {s.step_number}: {s.action_name}({s.action_params}) -> {s.result or s.thought or 'completed'}"
                    for s in state.steps
                )
                state.metadata["structured_output"] = summary
                state.final_output = summary
        except Exception as e:
            logger.warning(f"Pipeline outputter post-processing skipped: {e}")

        return state
