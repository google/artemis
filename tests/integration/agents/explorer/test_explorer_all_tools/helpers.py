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

"""Helper utilities for Explorer integration tests."""

from pathlib import Path
from PIL import Image
from unittest.mock import MagicMock
from artemis.context import ArtemisContext
from artemis.graph.state import State


def create_mock_context() -> MagicMock:
    """Creates a mock ArtemisContext suitable for Explorer tests."""
    ctx = MagicMock(spec=ArtemisContext)
    ctx.device = MagicMock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    ctx.llm_config = MagicMock()
    ctx.agent_config = MagicMock()
    ctx.agent_config.blacklisted_tools = {"explorer": []}
    ctx.data_engine = None
    ctx.adb_client = None
    return ctx


def create_mock_state() -> MagicMock:
    """Creates a mock State suitable for Explorer tests."""
    state = MagicMock(spec=State)
    state.operator_raw_data = {"width": 1080, "height": 2400}
    state.latest_ui_hierarchy = []
    state.latest_screenshot = None
    return state


def get_or_create_test_screenshot(preferred_path: Path | None = None) -> Path:
    """Finds an existing test screenshot or generates a valid 1080x2400 mock JPEG."""
    if preferred_path and preferred_path.exists() and preferred_path.stat().st_size > 0:
        return preferred_path

    # Search known repo screenshot paths
    candidate_paths = [
        Path(__file__).parent / "input_screenshot_test_explorer_all_tools_sequential_mocked.jpg",
        Path(__file__).parent / "input_screenshot_test_explorer_ask_vision_coder_tool.jpg",
        Path(__file__).parents[3] / "tools" / "inputs" / "screenshot.jpg",
    ]
    for p in candidate_paths:
        if p.exists() and p.stat().st_size > 0:
            return p

    # If none found, generate a mock RGB image
    out_path = (
        Path(__file__).parent / "input_screenshot_test_explorer_all_tools_sequential_mocked.jpg"
    )
    img = Image.new("RGB", (1080, 2400), color=(73, 109, 137))
    img.save(out_path, format="JPEG")
    return out_path
