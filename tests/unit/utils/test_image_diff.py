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
import cv2
from artemis.utils.image_diff import check_ui_change
import numpy as np


def test_no_change():
    img = np.zeros((1000, 1000), dtype=np.uint8)
    _, img_bytes = cv2.imencode(".jpg", img)

    action = {"action": "tap", "coordinates": [500, 500]}
    assert not check_ui_change(img_bytes.tobytes(), img_bytes.tobytes(), action)
    print("test_no_change passed")


def test_global_change():
    img1 = np.zeros((1000, 1000), dtype=np.uint8)
    img2 = np.ones((1000, 1000), dtype=np.uint8) * 255

    _, img1_bytes = cv2.imencode(".jpg", img1)
    _, img2_bytes = cv2.imencode(".jpg", img2)

    action = {"action": "swipe", "coordinates": [500, 500, 500, 200]}
    assert check_ui_change(img1_bytes.tobytes(), img2_bytes.tobytes(), action)
    print("test_global_change passed")


def test_roi_change():
    img1 = np.zeros((1000, 1000), dtype=np.uint8)
    img2 = img1.copy()
    img2[480:520, 480:520] = 255

    _, img1_bytes = cv2.imencode(".jpg", img1)
    _, img2_bytes = cv2.imencode(".jpg", img2)

    action = {"action": "tap", "coordinates": [500, 500]}
    assert check_ui_change(img1_bytes.tobytes(), img2_bytes.tobytes(), action)
    print("test_roi_change passed")


def test_status_bar_ignore():
    img1 = np.zeros((1000, 1000), dtype=np.uint8)
    img2 = img1.copy()
    img2[0:50, 0:1000] = 255

    _, img1_bytes = cv2.imencode(".jpg", img1)
    _, img2_bytes = cv2.imencode(".jpg", img2)

    action = {"action": "tap", "coordinates": [500, 500]}
    assert not check_ui_change(img1_bytes.tobytes(), img2_bytes.tobytes(), action)
    print("test_status_bar_ignore passed")


def test_roi_threshold():
    # Use standard 1080x2400 resolution to keep scale_factor = 1.0 (roi_size = 50)
    img1 = np.zeros((2400, 1080), dtype=np.uint8)
    img2 = img1.copy()
    # Center of 1080x2400 is (540, 1200). Draw 10x10 square at center.
    img2[1195:1205, 535:545] = 255

    _, img1_bytes = cv2.imencode(".jpg", img1)
    _, img2_bytes = cv2.imencode(".jpg", img2)

    action = {"action": "tap", "coordinates": [540, 1200]}
    assert not check_ui_change(
        img1_bytes.tobytes(),
        img2_bytes.tobytes(),
        action,
        full_screen_threshold=0.001,
        roi_threshold=0.05,
    )
    print("test_roi_threshold passed")


def test_wait_for_screen_stability():
    from artemis.utils.image_diff import wait_for_screen_stability

    img_static = np.zeros((1000, 1000), dtype=np.uint8)
    _, static_bytes = cv2.imencode(".jpg", img_static)
    static_b64 = base64.b64encode(static_bytes).decode("utf-8")

    # 1. Test immediate stability (already static)
    calls_immediate = 0

    async def mock_screenshot_immediate():
        nonlocal calls_immediate
        calls_immediate += 1
        return static_b64

    res_immediate = asyncio.run(
        wait_for_screen_stability(mock_screenshot_immediate, max_timeout=1.0, interval=0.05)
    )
    assert res_immediate == static_b64
    assert calls_immediate == 2
    print("test_wait_for_screen_stability: immediate stability subtest passed")

    # 2. Test gradual stability (settles after 3 moving frames)
    calls_gradual = 0

    async def mock_screenshot_gradual():
        nonlocal calls_gradual
        calls_gradual += 1
        if calls_gradual <= 3:
            img = np.zeros((1000, 1000), dtype=np.uint8)
            img[500:, :] = calls_gradual * 50
            _, bytes_data = cv2.imencode(".jpg", img)
            return base64.b64encode(bytes_data).decode("utf-8")
        return static_b64

    res_gradual = asyncio.run(
        wait_for_screen_stability(mock_screenshot_gradual, max_timeout=1.0, interval=0.05)
    )
    assert res_gradual == static_b64
    assert calls_gradual > 3
    print("test_wait_for_screen_stability: gradual stability subtest passed")
    print("test_wait_for_screen_stability passed")


def test_wait_for_screen_data_stability():
    from artemis.utils.image_diff import wait_for_screen_data_stability
    from artemis.controllers.device_controller import ScreenDataResponse

    img_static = np.zeros((1000, 1000), dtype=np.uint8)
    _, static_bytes = cv2.imencode(".jpg", img_static)
    static_b64 = base64.b64encode(static_bytes).decode("utf-8")

    # Test stability returning ScreenDataResponse
    calls = 0

    async def mock_get_screen_data():
        nonlocal calls
        calls += 1
        return ScreenDataResponse(
            base64=static_b64,
            elements=[],
            width=1000,
            height=1000,
            platform="android",
        )

    res = asyncio.run(
        wait_for_screen_data_stability(mock_get_screen_data, max_timeout=1.0, interval=0.05)
    )
    assert res.base64 == static_b64
    assert res.width == 1000
    assert calls == 2
    print("test_wait_for_screen_data_stability passed")


if __name__ == "__main__":
    try:
        test_no_change()
        test_global_change()
        test_roi_change()
        test_status_bar_ignore()
        test_roi_threshold()
        test_wait_for_screen_stability()
        test_wait_for_screen_data_stability()
        print("All tests passed successfully!")
    except AssertionError as e:
        print(f"Test failed: {e}")
    except Exception as e:
        print(f"Error running tests: {e}")
