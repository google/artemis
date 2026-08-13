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
from dotenv import load_dotenv

# Ensure we can import from project
sys.path.append(os.path.dirname(__file__))

from artemis.sdk import Agent
from artemis.sdk.builders import Builders
from artemis.sdk.types import AgentProfile

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_flash_standalone")


async def main():
    load_dotenv()

    # 1. Initialize Agent to set up device and connection pools
    logger.info("Initializing Agent and connecting to device...")
    profile = AgentProfile(name="default", from_file="llm-config.override.jsonc")
    config = Builders.AgentConfig.with_default_profile(profile).build()

    agent = Agent(config=config)
    await agent.init()

    # 2. Run task using 'flash' profile
    logger.info("Starting Flash runner task execution...")
    try:
        result = await agent.run_task(
            goal="Play a video in full screen on YouTube in Chrome", profile="flash"
        )
        logger.info(f"Flash runner task execution finished! Result: {result}")
    except Exception:
        logger.exception("Error during Flash task execution:")
    finally:
        await agent.clean()


if __name__ == "__main__":
    asyncio.run(main())
