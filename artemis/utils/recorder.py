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

import base64
import time

from langchain_core.messages import BaseMessage
from artemis.context import ArtemisContext
from artemis.controllers.controller_factory import create_device_controller
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


async def record_interaction(ctx: ArtemisContext, response: BaseMessage):
    if not ctx.execution_setup:
        raise ValueError("No execution setup found")
    if not ctx.execution_setup.traces_path or not ctx.execution_setup.trace_name:
        raise ValueError("No traces path or trace name found")

    logger.info("Recording interaction")
    controller = create_device_controller(ctx)
    screenshot_base64 = await controller.screenshot()
    logger.info("Screenshot taken")
    try:
        controller = create_device_controller(ctx)
        compressed_screenshot_base64 = controller.get_compressed_b64_screenshot(screenshot_base64)
    except Exception as e:
        logger.error(f"Error compressing screenshot: {e}")
        return "Could not record this interaction"
    timestamp = time.time()
    folder = ctx.execution_setup.traces_path.joinpath(ctx.execution_setup.trace_name).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with open(
            folder.joinpath(f"{int(timestamp)}.jpeg").resolve(),
            "wb",
        ) as f:
            f.write(base64.b64decode(compressed_screenshot_base64))

        with open(
            folder.joinpath(f"{int(timestamp)}.json").resolve(),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(response.model_dump_json())
    except Exception as e:
        logger.error(f"Error recording interaction: {e}")
    return "Screenshot recorded successfully"
