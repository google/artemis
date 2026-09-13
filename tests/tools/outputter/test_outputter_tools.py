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

"""The outputter mounts the shared history tools; smoke-test them against the
fixture database."""

import pytest

from artemis.context import ArtemisContext
from artemis.tools.history import get_history_tools


@pytest.fixture
def history_tools(artemis_context: ArtemisContext):
    search, replay, screenshot = get_history_tools(artemis_context)
    return {"search_history": search, "replay_steps": replay, "get_step_screenshot": screenshot}


def test_replay_steps_tool(history_tools):
    """replay_steps runs against the fixture history without errors."""
    result = history_tools["replay_steps"].invoke({"start_step": 1, "end_step": 5})

    assert isinstance(result, str)
    assert len(result) > 0


def test_get_step_screenshot_tool(history_tools):
    """get_step_screenshot returns content blocks with an image, or a plain explanation."""
    result = history_tools["get_step_screenshot"].invoke({"step_number": 1})

    if isinstance(result, list):
        assert len(result) == 2
        assert result[0]["type"] == "text"
        assert result[1]["type"] == "image_url"
    else:
        assert isinstance(result, str)
        assert len(result) > 0


def test_search_history_tool(history_tools):
    """search_history searches the fixture history and always answers in text."""
    result = history_tools["search_history"].invoke({"query": "test"})

    assert isinstance(result, str)
    assert len(result) > 0
