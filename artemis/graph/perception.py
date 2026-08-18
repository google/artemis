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
import hashlib
import json
from pathlib import Path

from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.data_engine.trace import trace
from artemis.graph.state import State
from artemis.utils.logger import get_logger
from artemis.utils.ocr_api import is_ocr_configured, perform_ocr
from artemis.utils.ocr_xml_fusion import (
    _crop_image_remove_status_bar,
    _detect_status_bar_height,
    _map_coordinates_back,
    fuse_ocr_with_xml,
)

logger = get_logger(__name__)


def _should_skip_settling(state: State) -> bool:
    """Determine if we should skip screen settling based on the last executed action.

    Purely logical, non-UI-drawing, or wait operations don't require dynamic
    settling checks.
    """
    if not state or not state.structured_decisions:
        # First turn or no prior action, skip settling
        return True

    try:
        actions = json.loads(state.structured_decisions)
        if not actions or not isinstance(actions, list):
            return True

        last_action = actions[-1]
        action_name = last_action.get("action")

        # Actions that are known to have already completed full wait loops or don't render UI
        skip_actions = {
            "wait_for_delay",
        }

        if action_name in skip_actions:
            logger.info(f"Last action was '{action_name}'. Skipping dynamic screen settling.")
            return True

        return False
    except Exception as e:
        logger.warning(
            f"Failed to parse last action for settling heuristic: {e}."
            " Defaulting to settling check."
        )
        return False


@trace(type="agent", name="perception")
def _check_injected_instruction_file(base_dir: str) -> str | None:
    instruction_path = Path(base_dir) / "injected_instruction.json"
    if instruction_path.exists():
        with open(instruction_path, encoding="utf-8") as f:
            data = json.load(f)
            injected_instruction = data.get("instruction")
        instruction_path.unlink()
        logger.info(f"Read and deleted injected instruction: '{injected_instruction}'")
        return injected_instruction
    return None


async def perception_node(state: State, ctx: ArtemisContext) -> dict:
    """Perception node (fast path): captures screenshot, extracts XML hierarchy, and performs OCR.

    Always executes at the start of an operator cycle.
    """
    logger.info("Entering Perception Node...")

    if not ctx.data_engine.current_step_id:
        ctx.data_engine.allocate_step_id()
        logger.info(f"Pre-allocated step ID in Perception Node: {ctx.data_engine.current_step_id}")

    # Check for injected instruction without blocking event loop
    injected_instruction = None
    if ctx.data_engine and ctx.data_engine.base_dir:
        injected_instruction = await asyncio.to_thread(
            _check_injected_instruction_file, str(ctx.data_engine.base_dir)
        )

    controller = UnifiedMobileController(ctx)

    # 1. Capture screenshot with hybrid settling strategy
    if _should_skip_settling(state):
        logger.info("Skipping settling. Retrieving screen data directly...")
        device_data = await controller.get_screen_data()
        latest_screenshot_b64 = device_data.base64
        xml_hierarchy = device_data.elements
    else:
        logger.info("Screen settling required. Delaying 0.4s and capturing screen data...")
        await asyncio.sleep(0.4)
        device_data = await controller.get_screen_data()
        latest_screenshot_b64 = device_data.base64
        xml_hierarchy = device_data.elements

    latest_screenshot_bytes = base64.b64decode(latest_screenshot_b64)

    # Update context device dimensions dynamically
    if ctx.device:
        ctx.device.device_width = device_data.width
        ctx.device.device_height = device_data.height
        logger.info(f"Updated context device dimensions: {device_data.width}x{device_data.height}")

    # 3. Perform OCR (if configured, else fallback to pure XML layout)
    ocr_results = []
    if is_ocr_configured():
        screen_height = device_data.height
        status_bar_height = _detect_status_bar_height(xml_hierarchy, screen_height)
        try:
            if status_bar_height > 0:
                logger.info(
                    f"Detected status bar height: {status_bar_height}. Cropping screenshot for OCR."
                )
                cropped_b64, _, _ = await asyncio.to_thread(
                    _crop_image_remove_status_bar,
                    latest_screenshot_b64,
                    status_bar_height,
                )
                raw_ocr_results = await perform_ocr(cropped_b64)
                ocr_results = _map_coordinates_back(raw_ocr_results, status_bar_height)
            else:
                logger.info("No status bar detected. Performing OCR on full screenshot.")
                ocr_results = await perform_ocr(latest_screenshot_b64)
        except Exception as ocr_err:
            logger.warning(
                f"Perception OCR execution encountered error: {ocr_err}. Proceeding with XML-only layout."
            )
            ocr_results = []
    else:
        logger.debug("OCR is not configured. Proceeding with XML-only layout.")

    # 4. Fuse OCR with XML (if ocr_results is empty, returns original xml_hierarchy intact)
    fused_xml = fuse_ocr_with_xml(xml_hierarchy, ocr_results)

    # 5. Save to Data Engine in background thread
    image_name = None
    screenshot_path = None
    if ctx.data_engine:
        image_name = hashlib.sha256(latest_screenshot_bytes).hexdigest()
        screenshot_path = str(ctx.data_engine.get_image_path(image_name))
        asyncio.create_task(
            asyncio.to_thread(
                ctx.data_engine.get_or_create_image,
                latest_screenshot_bytes,
                ui_tree=xml_hierarchy,
                ocr_result=ocr_results,
            )
        )
        logger.info(f"Offloaded perception data saving to background with image_name: {image_name}")

    # Cache latest perception data on ArtemisContext for downstream zero-latency reuse (e.g. Checker)
    ctx.latest_perception_data = {
        "screenshot_b64": latest_screenshot_b64,
        "latest_ui_hierarchy": fused_xml,
        "xml_hierarchy": xml_hierarchy if xml_hierarchy is not None else [],
        "ocr_results": ocr_results,
        "width": device_data.width,
        "height": device_data.height,
    }

    # Return state update
    update = {
        "latest_screenshot": screenshot_path,
        "latest_ui_hierarchy": fused_xml,
        "operator_raw_data": {
            "screenshot_b64": latest_screenshot_b64,
            "xml_hierarchy": xml_hierarchy if xml_hierarchy is not None else [],
            "ocr_results": ocr_results,
            "width": device_data.width,
            "height": device_data.height,
        },
        "injected_instruction": injected_instruction,
    }
    return update
