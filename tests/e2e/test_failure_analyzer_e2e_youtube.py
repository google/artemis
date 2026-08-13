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
import logging
import os
import sys
from unittest.mock import patch
from dotenv import load_dotenv

# Ensure we can import from project
sys.path.append(os.path.dirname(__file__))

from artemis.sdk import Agent
from artemis.sdk.builders import Builders
from artemis.sdk.types import AgentProfile
from artemis.agents.validator.validator import ValidatorNode, ValidationErrorCategory

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_fa_e2e")

# We want to fail the first validation to trigger the Failure Analyzer
original_validate = ValidatorNode._validate_action_precondition
original_validate_pixel = ValidatorNode._validate_action_precondition_pixel

has_failed_once = False


async def mock_validate_action_precondition(self, session, action_item, state=None):
    global has_failed_once
    if not has_failed_once:
        has_failed_once = True
        logger.warning(">>> MOCK VALIDATOR: Simulating pre-execution XML validation failure!")
        return (
            False,
            ValidationErrorCategory.TARGET_DISAPPEARED,
            "Simulated pre-execution XML validation failure",
        )
    return await original_validate(self, session, action_item, state=state)


async def mock_validate_action_precondition_pixel(
    self,
    session,
    action_item,
    pre_screenshot_b64,
    original_coords=None,
    state=None,
):
    global has_failed_once
    if not has_failed_once:
        has_failed_once = True
        logger.warning(">>> MOCK VALIDATOR: Simulating pre-execution Pixel validation failure!")
        return (
            False,
            ValidationErrorCategory.TARGET_DISAPPEARED,
            "Simulated pre-execution Pixel validation failure",
        )
    return await original_validate_pixel(
        self,
        session,
        action_item,
        pre_screenshot_b64,
        original_coords,
        state=state,
    )


async def main():
    load_dotenv()

    # Check if emulator/device is connected
    import subprocess

    res = subprocess.run(["adb", "devices"], capture_output=True, text=True)
    print("ADB Devices:")
    print(res.stdout)

    # 1. Initialize agent
    logger.info("Initializing Agent...")
    profile = AgentProfile(name="default", from_file="llm-config.override.jsonc")
    config = Builders.AgentConfig.with_default_profile(profile).build()

    agent = Agent(config=config)
    await agent.init()

    # 2. Run the task with patches
    goal = "Play a video in full screen on YouTube in Chrome"
    logger.info(f"Running task with goal: {goal}")

    with (
        patch.object(
            ValidatorNode,
            "_validate_action_precondition",
            mock_validate_action_precondition,
        ),
        patch.object(
            ValidatorNode,
            "_validate_action_precondition_pixel",
            mock_validate_action_precondition_pixel,
        ),
    ):
        try:
            result = await agent.run_task(goal=goal, name="test_fa_youtube_fullscreen")
            logger.info(f"Task finished! Result: {result}")
        except Exception:
            logger.exception("Error during execution:")
        finally:
            await agent.clean()


if __name__ == "__main__":
    asyncio.run(main())
