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
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import HumanMessage
from artemis.context import ArtemisContext
from artemis.data_engine.engine import DataEngine
from artemis.graph.state import State
from artemis.utils.logger import DataEngineHandler
import pytest


@pytest.fixture
def mock_artemis_ctx(tmp_path):
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_execution_setup = MagicMock()
    mock_execution_setup.traces_path = str(tmp_path)
    mock_ctx.execution_setup = mock_execution_setup
    mock_ctx.device = None
    return mock_ctx


@pytest.mark.asyncio
async def test_data_engine_handler_queue_buffering_and_background_worker(
    mock_artemis_ctx,
):
    """Verify DataEngineHandler.emit buffers log records via in-memory queue without spawning OS threads per log line."""
    engine = DataEngine(mock_artemis_ctx)
    session_id = engine.start_session("Test Log Queue")

    handler = DataEngineHandler()

    with patch("sys.modules") as mock_modules:
        mock_engine_mod = MagicMock()
        mock_engine_mod._CURRENT_DATA_ENGINE = engine
        mock_modules.get.return_value = mock_engine_mod

        # Emit 50 rapid log records
        for i in range(50):
            record = logging.LogRecord(
                name=f"test.logger.{i}",
                level=logging.INFO,
                pathname="test.py",
                lineno=10 + i,
                msg=f"Async queue test log message {i}",
                args=(),
                exc_info=None,
            )
            handler.emit(record)

    # Wait for the background worker thread of DataEngineHandler and DataEngine tasks to drain
    await asyncio.sleep(0.4)
    if engine._pending_tasks:
        await asyncio.gather(*engine._pending_tasks)

    # Verify that traces were recorded and persisted accurately via direct SQLite query
    with engine.storage._get_connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM traces WHERE session_id = ? AND type = 'log'",
            (str(session_id),),
        )
        log_rows = cursor.fetchall()

    assert len(log_rows) >= 50
    messages = {json.loads(row["payload"])["message"] for row in log_rows}
    for i in range(50):
        assert f"Async queue test log message {i}" in messages


@pytest.mark.asyncio
async def test_update_step_action_and_metadata_background_offloading(mock_artemis_ctx, tmp_path):
    """Verify that update_step_* methods publish SSE events instantly in memory and offload SQLite & disk I/O to background."""
    engine = DataEngine(mock_artemis_ctx)
    session_id = engine.start_session("Test Step Offloading")
    step_id = engine.allocate_step_id()

    # Create the initial step
    engine.record_step(summary="Initial step summary")
    if engine._pending_tasks:
        await asyncio.gather(*engine._pending_tasks)

    # Mock _publish to verify immediate real-time SSE delivery
    published_events = []
    engine._publish = lambda event_type, payload: published_events.append((event_type, payload))

    fake_action = {"action": "tap", "coordinates": [100, 200]}
    fake_post_jpg = b"fake_post_screenshot_jpg_bytes_12345"

    # Call update_step_action with post screenshot bytes
    engine.update_step_action(fake_action, post_screenshot_bytes=fake_post_jpg)

    # Verify SSE was published immediately before/during task execution
    assert len(published_events) == 1
    assert published_events[0][0] == "step_updated"
    assert published_events[0][1]["action_taken"] == fake_action

    # Call other update_step_* methods
    engine.update_step_summary(step_id, "Updated concise summary")
    engine.update_step_thinking(step_id, "Raw thinking process...")
    engine.update_step_native_thinking(step_id, "Native thinking process...")
    engine.update_step_execution_result(
        step_id, {"status": "success"}, post_image_name="hash_post_123"
    )

    # Now await all background pending tasks
    if engine._pending_tasks:
        await asyncio.gather(*engine._pending_tasks)

    # Verify SQLite record has been correctly updated by background tasks
    steps = engine.storage.get_steps(session_id)
    step_record = next((s for s in steps if s.step_id == step_id), None)
    assert step_record is not None
    assert step_record.action_taken == fake_action
    assert step_record.summary == "Updated concise summary"
    assert step_record.operator_raw_thinking == "Raw thinking process..."
    assert step_record.operator_native_thinking == "Native thinking process..."
    assert step_record.last_execution_result == {"status": "success"}
    assert step_record.post_image_name == "hash_post_123"

    # Verify post.jpg was written to disk inside step directory
    post_jpg_path = engine.current_step_dir / "post.jpg"
    assert post_jpg_path.exists()
    assert post_jpg_path.read_bytes() == fake_post_jpg


@pytest.mark.asyncio
async def test_record_step_background_image_and_step_persistence(
    mock_artemis_ctx,
):
    """Verify record_step immediately calculates SHA-256 hashes and offloads get_or_create_image + create_step to background."""
    engine = DataEngine(mock_artemis_ctx)
    session_id = engine.start_session("Test Record Step Image Offloading")
    step_id = engine.allocate_step_id()

    pre_img_bytes = b"fake_pre_screenshot_data_001"
    post_img_bytes = b"fake_post_screenshot_data_002"
    expected_pre_hash = hashlib.sha256(pre_img_bytes).hexdigest()
    expected_post_hash = hashlib.sha256(post_img_bytes).hexdigest()

    returned_step_id = engine.record_step(
        pre_screenshot_bytes=pre_img_bytes,
        post_screenshot_bytes=post_img_bytes,
        summary="Testing background image persistence",
    )
    assert returned_step_id == step_id

    # Await background storage tasks
    if engine._pending_tasks:
        await asyncio.gather(*engine._pending_tasks)

    # Verify image records were created in SQLite
    pre_record = engine.storage.get_image(expected_pre_hash)
    post_record = engine.storage.get_image(expected_post_hash)
    assert pre_record is not None
    assert pre_record.image_name == expected_pre_hash
    assert post_record is not None
    assert post_record.image_name == expected_post_hash

    # Verify image files exist on disk
    pre_path = engine.get_image_path(expected_pre_hash)
    post_path = engine.get_image_path(expected_post_hash)
    assert pre_path.exists() and pre_path.read_bytes() == pre_img_bytes
    assert post_path.exists() and post_path.read_bytes() == post_img_bytes

    # Verify StepRecord has correct hashes and summary
    steps = engine.storage.get_steps(session_id)
    step_record = next((s for s in steps if s.step_id == step_id), None)
    assert step_record is not None
    assert step_record.pre_image_name == expected_pre_hash
    assert step_record.post_image_name == expected_post_hash
    assert step_record.summary == "Testing background image persistence"


@pytest.mark.asyncio
async def test_perception_node_async_offloading(mock_artemis_ctx, tmp_path):
    """Verify perception_node offloads injected_instruction reading/unlinking and get_or_create_image to background."""
    from artemis.graph.perception import perception_node

    engine = DataEngine(mock_artemis_ctx)
    engine.start_session("Test Perception Node Offloading")
    mock_artemis_ctx.data_engine = engine

    # Create an injected_instruction.json file
    instruction_file = Path(engine.base_dir) / "injected_instruction.json"
    instruction_file.write_text(json.dumps({"instruction": "Avoid clicking Ads"}), encoding="utf-8")

    # Prepare mock controller
    fake_screenshot_bytes = b"fake_screenshot_for_perception_node_123"
    fake_screenshot_b64 = base64.b64encode(fake_screenshot_bytes).decode("utf-8")
    expected_hash = hashlib.sha256(fake_screenshot_bytes).hexdigest()

    mock_screen_data = MagicMock()
    mock_screen_data.base64 = fake_screenshot_b64
    mock_screen_data.elements = [{"resource-id": "app_bar", "bounds": "[0,0][1080,200]"}]
    mock_screen_data.width = 1080
    mock_screen_data.height = 2400

    with (
        patch("artemis.graph.perception.UnifiedMobileController") as mock_controller_cls,
        patch("artemis.graph.perception.perform_ocr", new_callable=AsyncMock) as mock_ocr,
        patch("artemis.graph.perception._detect_status_bar_height", return_value=0),
        patch("artemis.graph.perception._should_skip_settling", return_value=True),
    ):
        mock_controller = mock_controller_cls.return_value
        mock_controller.get_screen_data = AsyncMock(return_value=mock_screen_data)
        mock_ocr.return_value = [{"text": "Home", "box": [100, 100, 200, 200]}]

        state = State(
            initial_goal="test goal",
            latest_ui_hierarchy=[],
            latest_screenshot=None,
            structured_decisions=None,
        )

        update = await perception_node(state, mock_artemis_ctx)

    # Verify perception_node returned expected update immediately
    assert update["injected_instruction"] == "Avoid clicking Ads"
    assert update["latest_screenshot"] == str(engine.get_image_path(expected_hash))
    assert update["operator_raw_data"]["width"] == 1080

    # Verify injected_instruction.json was unlinked
    assert not instruction_file.exists()

    # Await background tasks triggered by perception_node
    await asyncio.sleep(0.1)
    if engine._pending_tasks:
        await asyncio.gather(*engine._pending_tasks)

    # Verify image record and file were created in background by perception_node
    img_record = engine.storage.get_image(expected_hash)
    assert img_record is not None
    assert img_record.image_name == expected_hash
    assert engine.get_image_path(expected_hash).exists()


@pytest.mark.asyncio
async def test_ocr_api_persistent_http_client_singleton_and_tls_reuse():
    """Verify get_http_client returns a persistent singleton httpx.AsyncClient that reuses TLS connections across OCR calls."""
    import httpx
    from artemis.utils import ocr_api

    # Reset singleton to test fresh creation & persistence
    ocr_api._HTTP_CLIENT = None
    client1 = ocr_api.get_http_client()
    client2 = ocr_api.get_http_client()

    assert isinstance(client1, httpx.AsyncClient)
    assert client1 is client2

    # Verify perform_ocr uses this exact persistent client
    fake_ocr_resp = MagicMock()
    fake_ocr_resp.status_code = 200
    fake_ocr_resp.json.return_value = {
        "responses": [
            {
                "textAnnotations": [
                    {
                        "description": "Combined text block",
                        "boundingPoly": {"vertices": []},
                    },
                    {
                        "description": "Hello World",
                        "boundingPoly": {"vertices": [{"x": 10, "y": 20}, {"x": 100, "y": 200}]},
                    },
                ]
            }
        ]
    }

    with (
        patch.object(client1, "post", new_callable=AsyncMock) as mock_post,
        patch.dict("os.environ", {"OCR_API_KEY": "mock_api_key"}),
    ):
        mock_post.return_value = fake_ocr_resp
        result = await ocr_api.perform_ocr(screenshot_b64="ZHVtbXlfYjY0")

        mock_post.assert_called_once()
        assert len(result) == 1
        assert result[0]["text"] == "Hello World"


@pytest.mark.asyncio
async def test_ui_filter_and_ui_automator_client_pre_parsed_bounds_o1_lookup():
    """Verify XML parsing pre-populates parsed_bounds and _parse_bounds hits this cache in O(1) without regex matches."""
    import re
    from artemis.clients.ui_automator_client import _parse_hierarchy_xml_to_elements
    from artemis.utils.ui_filter import _parse_bounds

    # 1. Test ingestion pre-parsing in _parse_hierarchy_xml_to_elements
    xml_data = '<hierarchy><node text="Login Btn" bounds="[15,25][350,120]"/></hierarchy>'
    elements = _parse_hierarchy_xml_to_elements(xml_data)
    assert len(elements) == 1
    assert elements[0]["parsed_bounds"] == {
        "left": 15,
        "top": 25,
        "right": 350,
        "bottom": 120,
    }

    # 2. Test _parse_bounds O(1) hit vs fallback
    with patch.object(re, "match", wraps=re.match) as spy_match:
        # Hit pre-parsed bounds -> re.match MUST NOT be called
        hit_result = _parse_bounds(elements[0])
        assert hit_result == {
            "left": 15,
            "top": 25,
            "right": 350,
            "bottom": 120,
        }
        spy_match.assert_not_called()

        # Fallback raw string -> re.match is called exactly once
        fallback_result = _parse_bounds("[50,60][150,160]")
        assert fallback_result == {
            "left": 50,
            "top": 60,
            "right": 150,
            "bottom": 160,
        }
        assert spy_match.call_count == 1


@pytest.mark.asyncio
async def test_validator_pre_execution_loop_reverted_to_exact_safety_contract(
    mock_artemis_ctx, tmp_path
):
    """Verify Validator pre-execution check runs VLM on XML failure, but retains XML error if VLM fails (reverted contract)."""
    from artemis.agents.validator.validator import ValidatorNode, ValidationErrorCategory

    # Set mock context properties required by tracing decorators
    mock_artemis_ctx.data_engine = MagicMock()

    node = ValidatorNode(mock_artemis_ctx)
    # The Validator now talks to the in-process unified action session.
    session = MagicMock()
    session.started = True
    session.screenshot_b64 = AsyncMock(return_value="ZHVtbXlfZGF0YQ==")
    session.ui_hierarchy = AsyncMock(return_value=[])
    session.call = AsyncMock()

    decisions = json.dumps(
        [
            {
                "action": "tap",
                "coordinates": [200, 250],
                "target_text": "Login",
                "target_bounds": [100, 200, 300, 300],
                "target_resource_id": "btn_login",
            }
        ]
    )
    state = State(
        initial_goal="test",
        latest_ui_hierarchy=[],
        latest_screenshot="dummy.png",
        structured_decisions=decisions,
    )

    with (
        patch(
            "artemis.agents.validator.validator.get_action_session",
            AsyncMock(return_value=session),
        ),
        patch.object(
            ValidatorNode, "_get_initial_screenshot", new_callable=AsyncMock
        ) as mock_get_screen,
        patch.object(
            ValidatorNode, "_validate_action_precondition", new_callable=AsyncMock
        ) as mock_xml,
        patch.object(
            ValidatorNode,
            "_validate_action_precondition_pixel",
            new_callable=AsyncMock,
        ) as mock_pixel,
    ):
        mock_get_screen.return_value = (
            base64.b64encode(b"dummy_bytes_here").decode("utf-8"),
            "dummy_stem",
        )
        # 1. XML check returns failure (TARGET_OCCUPIED by 'Sign Up' button)
        mock_xml.return_value = (
            False,
            ValidationErrorCategory.TARGET_OCCUPIED,
            "Occupied by Sign Up button",
        )
        # 2. VLM check returns failure (PIXEL_TARGET_DISAPPEARED)
        mock_pixel.return_value = (
            False,
            ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED,
            "VLM target missing",
        )

        result = await node(state)

        # Verify both XML check and VLM check were called (reverted double-check contract)
        mock_xml.assert_called_once()
        mock_pixel.assert_called_once()

        # The incident carries the EXACT XML error (TARGET_OCCUPIED), NOT the VLM error
        incident = result["last_execution_result"]["incident"]
        assert incident["kind"] == "safety_net"
        assert incident["category"] == ValidationErrorCategory.TARGET_OCCUPIED.value
        assert "Occupied by Sign Up button" in incident["reason"]
        assert result["open_incident"] == incident


@pytest.mark.asyncio
async def test_record_step_pre_allocated_id_reset(mock_artemis_ctx):
    """Verify that after record_step is called, the pre-allocated current_step_id is reset to None so subsequent steps generate distinct IDs."""
    from artemis.data_engine.engine import DataEngine

    engine = DataEngine(mock_artemis_ctx)
    engine.start_session("Pre-allocated ID Reset Test")

    # 1. Pre-allocate ID for step 1
    step_id_1 = engine.allocate_step_id()
    assert engine.current_step_id == step_id_1

    # 2. Record step 1 (should use step_id_1 and reset current_step_id to None)
    returned_id_1 = engine.record_step(summary="Step 1")
    assert returned_id_1 == step_id_1
    assert engine.current_step_id is None

    # 3. Pre-allocate ID for step 2 (should get a distinct new ID)
    step_id_2 = engine.allocate_step_id()
    assert step_id_2 != step_id_1
    assert engine.current_step_id == step_id_2

    # 4. Record step 2 (should use step_id_2 and reset current_step_id to None)
    returned_id_2 = engine.record_step(summary="Step 2")
    assert returned_id_2 == step_id_2
    assert returned_id_2 != returned_id_1
    assert engine.current_step_id is None
