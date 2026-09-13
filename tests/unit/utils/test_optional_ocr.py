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

"""Unit tests verifying that OCR is optional and the system degrades gracefully."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from artemis.agents.explorer.explorer import Explorer
from artemis.config.settings import Settings
from artemis.graph.perception import perception_node
from artemis.graph.state import State
from artemis.tools.index import get_tools_from_wrappers
from artemis.tools.mobile.ocr import (
    ocr_recognition,
    ocr_recognition_wrapper,
)
from artemis.utils.ocr_api import is_ocr_configured, perform_ocr
from artemis.utils.ocr_xml_fusion import fuse_ocr_with_xml
from artemis.utils.visualization import format_minimal_list_with_elements


@pytest.mark.asyncio
async def test_is_ocr_configured_false_when_unset(monkeypatch):
    """Verify is_ocr_configured returns False when keys are unset or placeholder."""
    monkeypatch.delenv("OCR_API_KEY", raising=False)
    monkeypatch.delenv("VISION_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    with patch.object(Settings, "get_api_key", return_value=None):
        assert not is_ocr_configured()


@pytest.mark.asyncio
async def test_perform_ocr_returns_empty_list_when_unconfigured(monkeypatch):
    """Verify perform_ocr returns [] when no OCR key is present."""
    with (
        patch.object(Settings, "get_api_key", return_value=None),
        patch.dict("os.environ", {}, clear=True),
    ):
        result = await perform_ocr(screenshot_b64="dummy_b64")
        assert result == []


@pytest.mark.asyncio
async def test_perform_ocr_executes_api_call_when_configured():
    """Verify perform_ocr makes API request and parses responses when configured."""
    mock_key = MagicMock()
    mock_key.get_secret_value.return_value = "test_key"

    with (
        patch.object(Settings, "get_api_key", return_value=mock_key),
        patch("artemis.utils.ocr_api.get_http_client") as mock_client,
    ):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "responses": [
                {
                    "textAnnotations": [
                        {"description": "Full text\nLogin", "boundingPoly": {}},
                        {
                            "description": "Login",
                            "boundingPoly": {
                                "vertices": [{"x": 100, "y": 200}, {"x": 300, "y": 250}]
                            },
                        },
                    ]
                }
            ]
        }
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_resp
        mock_client.return_value = mock_http

        result = await perform_ocr(screenshot_b64="dummy_b64")
        assert len(result) == 1
        assert result[0]["text"] == "Login"


def test_fuse_ocr_with_xml_empty_ocr_retains_xml_pruning():
    """Verify fuse_ocr_with_xml preserves full XML structure and allows downstream pruning."""
    sample_xml = [
        {
            "class": "android.widget.TextView",
            "text": "Submit Button",
            "bounds": "[100,200][300,250]",
        },
        {
            "class": "android.widget.ImageView",
            "content-desc": "Settings Icon",
            "bounds": "[900,50][980,130]",
        },
    ]

    # Fusing with empty OCR results
    fused_xml = fuse_ocr_with_xml(sample_xml, [])
    assert len(fused_xml) == 2
    assert fused_xml[0]["text"] == "Submit Button"

    # Downstream XML formatting / pruning
    minimal_list, elements, labels = format_minimal_list_with_elements(
        fused_xml, width=1080, height=2400
    )
    assert len(elements) == 2
    assert "Submit Button" in minimal_list
    assert "Settings Icon" in minimal_list
    assert labels == ["1", "2"]


def test_ocr_tool_not_exposed_when_unconfigured():
    """Verify ocr_recognition tool is hidden and not exposed when OCR is unconfigured."""
    with patch("artemis.tools.mobile.ocr.is_ocr_configured", return_value=False):
        assert not ocr_recognition.is_available()

        # get_tools_from_wrappers excludes ocr_recognition_wrapper
        mock_ctx = MagicMock()
        wrapped_tools = get_tools_from_wrappers(mock_ctx, [ocr_recognition_wrapper])
        assert len(wrapped_tools) == 0


def test_explorer_does_not_expose_get_ocr_list_when_unconfigured():
    """Verify Explorer does not expose get_ocr_list tool declaration when OCR is unconfigured."""
    with patch("artemis.agents.explorer.explorer.is_ocr_configured", return_value=False):
        mock_ctx = MagicMock()
        mock_ctx.agent_config = None
        explorer = Explorer(mock_ctx)
        explorer.denylisted_tools = set()

        exposed = explorer.get_exposed_tools()
        exposed_names = [t.name for t in exposed]
        assert "get_ocr_list" not in exposed_names


@pytest.mark.asyncio
async def test_perception_node_runs_smoothly_without_ocr():
    """Verify perception_node runs from start to finish with pure XML when OCR is unconfigured."""
    with (
        patch("artemis.graph.perception.is_ocr_configured", return_value=False),
        patch("artemis.graph.perception.UnifiedMobileController") as mock_controller_cls,
    ):
        mock_controller = MagicMock()
        mock_device_data = MagicMock()
        mock_device_data.width = 1080
        mock_device_data.height = 2400
        mock_device_data.base64 = "ZHVtbXk="
        mock_device_data.bytes = b"dummy"
        mock_device_data.elements = [
            {
                "class": "android.widget.Button",
                "text": "Login",
                "bounds": "[100,500][400,600]",
            }
        ]
        mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)
        mock_controller_cls.return_value = mock_controller

        mock_ctx = MagicMock()
        mock_ctx.data_engine = MagicMock()
        mock_ctx.data_engine.current_step_id = "step_1"
        mock_ctx.device = MagicMock()
        mock_ctx.device.device_width = 1080
        mock_ctx.device.device_height = 2400

        mock_state = MagicMock(spec=State)
        mock_state.structured_decisions = []

        update = await perception_node(mock_state, mock_ctx)
        assert update is not None
        assert "latest_ui_hierarchy" in update
        assert len(update["latest_ui_hierarchy"]) == 1
        assert update["latest_ui_hierarchy"][0]["text"] == "Login"
        assert update["operator_raw_data"]["ocr_results"] == []
