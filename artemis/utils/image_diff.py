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
from collections.abc import Awaitable, Callable
import logging
import math
import time

import cv2
import numpy as np

logger = logging.getLogger(__name__)


def check_ui_change(
    img_before_bytes,
    img_after_bytes,
    action_item,
    full_screen_threshold=0.0001,
    roi_threshold=0.05,
):
    """Checks if the UI has changed after an action using image diff.

    Args:
        img_before_bytes: Image bytes before action.
        img_after_bytes: Image bytes after action.
        action_item: Dict containing 'action' and optionally 'coordinates'.
        full_screen_threshold: Threshold for full screen change (fraction of
          changed pixels).
        roi_threshold: Threshold for ROI change (fraction of changed pixels in
          ROI).

    Returns:
        bool: True if UI changed, False otherwise.
    """
    try:
        # 1. Decode images
        nparr_b = np.frombuffer(img_before_bytes, np.uint8)
        nparr_a = np.frombuffer(img_after_bytes, np.uint8)
        img_b = cv2.imdecode(nparr_b, cv2.IMREAD_GRAYSCALE)
        img_a = cv2.imdecode(nparr_a, cv2.IMREAD_GRAYSCALE)

        if img_b is None or img_a is None:
            logger.error("Failed to decode images.")
            return True  # Fallback to assuming change if we can't decode

        h, w = img_b.shape
        action_name = action_item.get("action")
        coordinates = action_item.get("coordinates")

        # 2. Masking: Ignore status bar (top 10%) and nav bar (bottom 5%)
        top_crop = int(h * 0.10)
        bottom_crop = int(h * 0.05)

        content_b = img_b[top_crop : h - bottom_crop, :]
        content_a = img_a[top_crop : h - bottom_crop, :]

        # 3. Calculate absolute difference and threshold
        diff = cv2.absdiff(content_b, content_a)
        blurred = cv2.GaussianBlur(diff, (5, 5), 0)
        _, thresh = cv2.threshold(blurred, 25, 255, cv2.THRESH_BINARY)

        non_zero_count = cv2.countNonZero(thresh)
        total_pixels = content_b.shape[0] * content_b.shape[1]
        global_change_ratio = non_zero_count / total_pixels

        logger.info(f"Action: {action_name} | Global UI change ratio: {global_change_ratio:.4f}")

        # 4. Strategy based on action type
        if action_name == "swipe":
            # Swipe expects large scale movement
            return global_change_ratio > full_screen_threshold

        # For tap and others, check ROI if coordinates are available
        if (
            action_name
            in [
                "tap",
                "long_press_on",
                "focus_and_input_text",
                "focus_and_clear_text",
            ]
            and coordinates
            and len(coordinates) == 2
        ):
            cx, cy = coordinates
            # Scale ROI size dynamically based on screen diagonal (reference is 1080x2400)
            # and enforce a minimum of 15px to prevent collapsing on low-res screens
            ref_diagonal = math.sqrt(1080**2 + 2400**2)
            current_diagonal = math.sqrt(w**2 + h**2)
            scale_factor = current_diagonal / ref_diagonal
            roi_size = max(15, int(50 * scale_factor))

            # Crop ROI from original images to avoid shifting issues with top_crop
            y1 = max(0, cy - roi_size)
            y2 = min(h, cy + roi_size)
            x1 = max(0, cx - roi_size)
            x2 = min(w, cx + roi_size)

            roi_b = img_b[y1:y2, x1:x2]
            roi_a = img_a[y1:y2, x1:x2]

            roi_diff = cv2.absdiff(roi_b, roi_a)
            roi_blurred = cv2.GaussianBlur(roi_diff, (3, 3), 0)
            _, roi_thresh = cv2.threshold(roi_blurred, 20, 255, cv2.THRESH_BINARY)

            roi_non_zero = cv2.countNonZero(roi_thresh)
            roi_total = roi_b.shape[0] * roi_b.shape[1]
            roi_change_ratio = roi_non_zero / roi_total

            logger.info(f"ROI change ratio at [{cx}, {cy}]: {roi_change_ratio:.4f}")

            # If ROI changed significantly, we assume action worked
            if roi_change_ratio > roi_threshold:
                return True

        # Fallback to global check if no ROI check was triggered or passed
        return global_change_ratio > full_screen_threshold

    except Exception as e:
        logger.error(f"Error during image diff: {e}")
        return True  # Fallback to assuming change on error


async def wait_for_screen_stability(
    take_screenshot_fn: Callable[[], Awaitable[str]],
    max_timeout: float = 1.5,
    interval: float = 0.2,
    stability_threshold: float = 0.001,
) -> str:
    """Wait for the screen to stabilize (nearly zero pixel changes between consecutive frames)

    before returning the latest base64 screenshot.

    Args:
        take_screenshot_fn: Async callback function that captures a base64
          screenshot.
        max_timeout: Maximum time to wait for settling (in seconds) before
          forcing exit.
        interval: Polling time step between screenshot samples (in seconds).
        stability_threshold: Pixel change ratio below which the screen is
          considered settled (e.g. 0.001 for < 0.1%).

    Returns:
        str: The final settled base64 screenshot.
    """
    logger.info("Starting Dynamic Screen Settling...")

    # 1. Capture the initial frame
    try:
        last_b64 = await take_screenshot_fn()
        last_bytes = base64.b64decode(last_b64)
    except Exception as e:
        logger.error(f"Failed to capture initial frame for settling: {e}")
        # Return fallback immediately on failure
        return await take_screenshot_fn()

    start_time = time.time()
    while time.time() - start_time < max_timeout:
        await asyncio.sleep(interval)

        try:
            curr_b64 = await take_screenshot_fn()
            curr_bytes = base64.b64decode(curr_b64)
        except Exception as e:
            logger.warning(f"Failed to capture polling frame: {e}")
            continue

        # 2. Decode and compare using OpenCV
        try:
            nparr_b = np.frombuffer(last_bytes, np.uint8)
            nparr_a = np.frombuffer(curr_bytes, np.uint8)
            img_b = cv2.imdecode(nparr_b, cv2.IMREAD_GRAYSCALE)
            img_a = cv2.imdecode(nparr_a, cv2.IMREAD_GRAYSCALE)

            if img_b is None or img_a is None:
                last_b64, last_bytes = curr_b64, curr_bytes
                continue

            h, w = img_b.shape

            # Masking: crop status bar (top 10%) and navigation bar (bottom 5%)
            # to avoid false positives from notification icons, clocks, or blinking elements
            top_crop = int(h * 0.10)
            bottom_crop = int(h * 0.05)

            content_b = img_b[top_crop : h - bottom_crop, :]
            content_a = img_a[top_crop : h - bottom_crop, :]

            # Calculate absolute difference and filter noise
            diff = cv2.absdiff(content_b, content_a)
            blurred = cv2.GaussianBlur(diff, (5, 5), 0)
            _, thresh = cv2.threshold(blurred, 25, 255, cv2.THRESH_BINARY)

            non_zero_count = cv2.countNonZero(thresh)
            total_pixels = content_b.shape[0] * content_b.shape[1]
            change_ratio = non_zero_count / total_pixels

            elapsed = time.time() - start_time
            logger.info(
                f"Screen settle poll: diff ratio = {change_ratio:.4f} (target <"
                f" {stability_threshold}) | Elapsed: {elapsed:.2f}s"
            )

            # If the change ratio is below stability threshold, screen has stabilized!
            if change_ratio < stability_threshold:
                logger.info(f"Screen stabilized after {elapsed:.2f}s (diff: {change_ratio:.4f}).")
                return curr_b64

            last_b64, last_bytes = curr_b64, curr_bytes

        except Exception as ex:
            logger.error(f"Error during screen stability image diff calculation: {ex}")
            last_b64, last_bytes = curr_b64, curr_bytes
            continue

    # 3. Timeout safety fallback (e.g., dynamic playing video/animated loaders)
    logger.warning(
        f"Screen settle timed out after {max_timeout}s without complete"
        " stability. Forcing return of latest frame."
    )
    return last_b64


async def wait_for_screen_data_stability(
    get_screen_data_fn,
    max_timeout: float = 1.5,
    interval: float = 0.2,
    stability_threshold: float = 0.001,
):
    """Wait for the screen to stabilize (nearly zero pixel changes between consecutive frames)

    while fetching both screenshot and XML hierarchy directly
    (ScreenDataResponse).

    Args:
        get_screen_data_fn: Async callback function that captures a
          ScreenDataResponse.
        max_timeout: Maximum time to wait for settling (in seconds) before
          forcing exit.
        interval: Polling time step between screen data samples (in seconds).
        stability_threshold: Pixel change ratio below which the screen is
          considered settled.

    Returns:
        ScreenDataResponse: The final settled ScreenDataResponse.
    """
    logger.info("Starting Dynamic Screen Data Settling...")

    # 1. Capture the initial frame
    try:
        last_data = await get_screen_data_fn()
        last_bytes = base64.b64decode(last_data.base64)
    except Exception as e:
        logger.error(f"Failed to capture initial frame for settling: {e}")
        # Return fallback immediately on failure
        return await get_screen_data_fn()

    start_time = time.time()
    while time.time() - start_time < max_timeout:
        await asyncio.sleep(interval)

        try:
            curr_data = await get_screen_data_fn()
            curr_bytes = base64.b64decode(curr_data.base64)
        except Exception as e:
            logger.warning(f"Failed to capture polling frame: {e}")
            continue

        # 2. Decode and compare using OpenCV
        try:
            nparr_b = np.frombuffer(last_bytes, np.uint8)
            nparr_a = np.frombuffer(curr_bytes, np.uint8)
            img_b = cv2.imdecode(nparr_b, cv2.IMREAD_GRAYSCALE)
            img_a = cv2.imdecode(nparr_a, cv2.IMREAD_GRAYSCALE)

            if img_b is None or img_a is None:
                last_data, last_bytes = curr_data, curr_bytes
                continue

            h, w = img_b.shape

            # Masking: crop status bar (top 10%) and navigation bar (bottom 5%)
            top_crop = int(h * 0.10)
            bottom_crop = int(h * 0.05)

            content_b = img_b[top_crop : h - bottom_crop, :]
            content_a = img_a[top_crop : h - bottom_crop, :]

            # Calculate absolute difference and filter noise
            diff = cv2.absdiff(content_b, content_a)
            blurred = cv2.GaussianBlur(diff, (5, 5), 0)
            _, thresh = cv2.threshold(blurred, 25, 255, cv2.THRESH_BINARY)

            non_zero_count = cv2.countNonZero(thresh)
            total_pixels = content_b.shape[0] * content_b.shape[1]
            change_ratio = non_zero_count / total_pixels

            elapsed = time.time() - start_time
            logger.info(
                f"Screen data settle poll: diff ratio = {change_ratio:.4f}"
                f" (target < {stability_threshold}) | Elapsed: {elapsed:.2f}s"
            )

            # If the change ratio is below stability threshold, screen has stabilized!
            if change_ratio < stability_threshold:
                logger.info(f"Screen stabilized after {elapsed:.2f}s (diff: {change_ratio:.4f}).")
                return curr_data

            last_data, last_bytes = curr_data, curr_bytes

        except Exception as ex:
            logger.error(f"Error during screen stability image diff calculation: {ex}")
            last_data, last_bytes = curr_data, curr_bytes
            continue

    # 3. Timeout safety fallback
    logger.warning(
        f"Screen data settle timed out after {max_timeout}s without complete"
        " stability. Forcing return of latest frame."
    )
    return last_data
