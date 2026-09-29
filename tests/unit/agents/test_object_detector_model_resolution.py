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

"""The object detector must resolve its model from the ``utils.object_detector`` config."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.agents.object_detector import object_detector as od

_MODULE = "artemis.agents.object_detector.object_detector"


@pytest.mark.asyncio
async def test_detector_uses_the_utils_object_detector_model():
    detector_llm = MagicMock(name="object_detector_llm")
    get_llm = MagicMock(return_value=detector_llm)
    detect = AsyncMock(return_value=[])

    with patch(f"{_MODULE}.get_llm", get_llm), patch(f"{_MODULE}._detect_single_label", detect):
        await od._run_object_detection(MagicMock(), image_bytes=b"img", queries=["OK button"])

    get_llm.assert_called_once()
    assert get_llm.call_args.kwargs.get("name") == "object_detector"
    assert get_llm.call_args.kwargs.get("is_utils") is True
    assert detect.call_args.args[0] is detector_llm


@pytest.mark.asyncio
async def test_detector_falls_back_to_the_operator_model_with_a_warning():
    operator_llm = MagicMock(name="operator_llm")

    def fake_get_llm(ctx, *, name, **kwargs):
        if name == "object_detector":
            raise ValueError("detector model not configured")
        return operator_llm

    detect = AsyncMock(return_value=[])
    with (
        patch(f"{_MODULE}.get_llm", side_effect=fake_get_llm),
        patch(f"{_MODULE}._detect_single_label", detect),
        patch(f"{_MODULE}.logger") as logger,
    ):
        await od._run_object_detection(MagicMock(), image_bytes=b"img", queries=["OK button"])

    assert detect.call_args.args[0] is operator_llm
    logger.warning.assert_called_once()
    assert "operator" in logger.warning.call_args.args[0]
