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
import base64
import logging
import os
import sys
from dotenv import load_dotenv

# Ensure we can import from project
sys.path.append(os.path.dirname(__file__))

from artemis.sdk import Agent
from artemis.sdk.builders import Builders
from artemis.sdk.types import AgentProfile
from artemis.context import ArtemisContext
from artemis.graph.state import State
from artemis.agents.validator.failure_analyzer import FailureAnalyzer, ValidationErrorCategory
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.utils.notes import get_note_file_path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_fa_standalone")


async def main():
    load_dotenv()

    # 1. Initialize Agent to set up device and connection pools
    logger.info("Initializing Agent and connecting to device...")
    profile = AgentProfile(name="default", from_file="llm-config.override.jsonc")
    config = Builders.AgentConfig.with_default_profile(profile).build()

    agent = Agent(config=config)
    await agent.init()

    # Create the ArtemisContext manually using agent's initialized clients
    context = ArtemisContext(
        device=agent._device_context,
        adb_client=agent._adb_client,
        ui_adb_client=agent._ui_adb_client,
        llm_config=profile.llm_config,
        agent_config=config,
    )

    # Set up trace session and data engine
    from artemis.data_engine.engine import DataEngine

    data_engine = DataEngine(ctx=context)
    context.data_engine = data_engine
    data_engine.start_session(goal="Play a video in full screen on YouTube in Chrome")

    # Write the task plan note
    task_plan_content = (
        "- [ ] Open Chrome and navigate to the YouTube website (youtube.com)\n"
        "- [ ] Select and play any video\n"
        "- [ ] Make the playing video full screen"
    )
    task_plan_path = get_note_file_path(data_engine.base_dir, "task_plan")
    task_plan_path.parent.mkdir(parents=True, exist_ok=True)
    task_plan_path.write_text(task_plan_content, encoding="utf-8")

    # 2. Get initial screenshot
    controller = UnifiedMobileController(context)
    screen_data = await controller.controller.get_screen_data()
    screenshot_b64 = screen_data.base64

    # Set up State
    state = State(
        initial_goal="Play a video in full screen on YouTube in Chrome",
        latest_ui_hierarchy=screen_data.elements,
        latest_screenshot=str(
            data_engine.get_image_path(
                data_engine.get_or_create_image(base64.b64decode(screenshot_b64))
            )
        ),
        structured_decisions="",
    )

    # 3. Instantiate and run Failure Analyzer directly
    logger.info("Starting Failure Analyzer directly...")
    analyzer = FailureAnalyzer(context)
    analyzer.max_iterations = 45

    # We pass a dummy failed action representing the fullscreen button tap
    dummy_failed_action = {
        "action": "tap",
        "coordinates": [968, 883],
        "target_text": "fullscreen button",
    }

    try:
        result = await analyzer.analyze(
            state=state,
            failed_action=dummy_failed_action,
            error_msg="Simulated launch failure to trigger standalone FA run",
            pre_screenshot=screenshot_b64,
            post_screenshot=screenshot_b64,
            error_category=ValidationErrorCategory.TARGET_DISAPPEARED,
        )
        logger.info(f"Failure Analyzer direct execution finished! Result: {result}")
    except Exception:
        logger.exception("Error during Failure Analyzer execution:")
    finally:
        await agent.clean()


if __name__ == "__main__":
    asyncio.run(main())
