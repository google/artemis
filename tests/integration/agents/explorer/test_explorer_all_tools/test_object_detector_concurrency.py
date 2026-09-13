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
import time
from unittest.mock import AsyncMock, MagicMock, patch

from artemis.agents.object_detector.object_detector import _run_object_detection
from tests.integration.agents.explorer.test_explorer_all_tools.helpers import create_mock_context
import pytest


@pytest.mark.asyncio
async def test_object_detector_concurrency_and_throttling(tmp_path):
    """Verify that object detector requests run in parallel and are correctly throttled by the semaphore."""
    # Create a dummy image file for the test
    dummy_image_path = tmp_path / "dummy.jpg"
    dummy_image_path.write_bytes(b"dummy image data")

    # Setup mock context
    mock_ctx = create_mock_context()

    # Configure the object detector LLM config explicitly to avoid MagicMocks for timeout and model
    od_cfg = MagicMock()
    od_cfg.model = "gemini-robotics-er-early-access"
    od_cfg.fix_model = "gemini-2.5-flash"
    od_cfg.timeout = 10.0
    mock_ctx.llm_config.utils.object_detector = od_cfg

    # Track active requests
    active_requests = 0
    max_concurrent_requests = 0
    request_lock = asyncio.Lock()
    from langchain_core.messages import AIMessage

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=lambda *args, **kwargs: mock_generate_content())

    async def mock_generate_content(*args, **kwargs):
        nonlocal active_requests, max_concurrent_requests
        async with request_lock:
            active_requests += 1
            if active_requests > max_concurrent_requests:
                max_concurrent_requests = active_requests

        try:
            await asyncio.sleep(0.1)
        finally:
            async with request_lock:
                active_requests -= 1

        return AIMessage(content='[{"point": [500, 500]}]')

    mock_llm.ainvoke = AsyncMock(side_effect=mock_generate_content)

    with patch(
        "artemis.agents.object_detector.object_detector.get_llm",
        return_value=mock_llm,
    ):
        # --- TEST 1: Concurrency Assertion (5 labels) ---
        # 5 labels should run in parallel, completing in ~0.1s (much faster than a sequential 0.5s)
        # Reset trackers
        active_requests = 0
        max_concurrent_requests = 0

        start_time = time.time()
        labels_5 = [f"label_{i}" for i in range(5)]
        templates = ["Find {labels_str}"]

        result_5 = await _run_object_detection(
            ctx=mock_ctx,
            image_path=str(dummy_image_path),
            queries=labels_5,
            templates=templates,
        )
        elapsed_5 = time.time() - start_time

        # Concurrency assertions
        assert max_concurrent_requests == 5, (
            f"Expected 5 concurrent requests, got {max_concurrent_requests}"
        )
        assert elapsed_5 < 0.3, f"Expected execution to take < 0.3s, but took {elapsed_5:.3f}s"

        # Verify result structure and swapped coordinates [y, x] -> [x, y]
        parsed_result_5 = result_5
        assert len(parsed_result_5["detected"]) == 5
        for item in parsed_result_5["detected"]:
            assert item["point"] == [500, 500]

        # --- TEST 2: Throttling Assertion (10 labels) ---
        # 10 labels should be throttled by the semaphore limit of 6.
        # It should run in two batches, completing in ~0.2s (much faster than a sequential 1.0s).
        # Reset trackers
        active_requests = 0
        max_concurrent_requests = 0

        start_time = time.time()
        labels_10 = [f"label_{i}" for i in range(10)]

        result_10 = await _run_object_detection(
            ctx=mock_ctx,
            image_path=str(dummy_image_path),
            queries=labels_10,
            templates=templates,
        )
        elapsed_10 = time.time() - start_time

        # Throttling assertions
        assert max_concurrent_requests == 6, (
            f"Expected max concurrent requests to be capped at 6, got {max_concurrent_requests}"
        )
        assert elapsed_10 < 0.5, (
            f"Expected execution to take < 0.5s (two batches of 0.1s), but took {elapsed_10:.3f}s"
        )

        parsed_result_10 = result_10
        assert len(parsed_result_10["detected"]) == 10
        for item in parsed_result_10["detected"]:
            assert item["point"] == [500, 500]
