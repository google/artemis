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

"""Every way a history tool fails to do what was asked is reported
structurally (a ``ToolFailure``), never as a plain string the caller would
have to sniff: no history, a usage error, or an exception from the reader."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from artemis.core.tool_failure import is_tool_failure
from artemis.tools.history import (
    GetStepScreenshotTool,
    ReplayStepsTool,
    SearchHistoryTool,
)


def _ctx():
    return SimpleNamespace(data_engine=MagicMock())


@pytest.mark.asyncio
async def test_search_history_reader_exception_is_a_tool_failure():
    with patch(
        "artemis.tools.history.search_history_text", side_effect=RuntimeError("index corrupt")
    ):
        out = await SearchHistoryTool().execute(ctx=_ctx(), query="login")
    assert is_tool_failure(out)
    assert out == "search_history failed: index corrupt"


@pytest.mark.asyncio
async def test_search_history_usage_error_is_a_tool_failure():
    """Asking for neither a query nor a range is a request the tool did not
    serve: the guidance is the failure text the model reads."""
    out = await SearchHistoryTool().execute(ctx=_ctx(), query="", step_range=None)
    assert is_tool_failure(out)
    assert "needs a query and/or a step_range" in out


@pytest.mark.asyncio
async def test_replay_steps_reader_exception_is_a_tool_failure():
    with patch("artemis.tools.history.replay_steps_text", side_effect=OSError("db gone")):
        out = await ReplayStepsTool().execute(ctx=_ctx(), start_step=3)
    assert is_tool_failure(out)
    assert out == "replay_steps failed: db gone"


@pytest.mark.asyncio
async def test_get_step_screenshot_exception_is_a_tool_failure():
    with patch(
        "artemis.tools.history.load_step_screenshot",
        side_effect=FileNotFoundError("no screenshot for step 7"),
    ):
        out = await GetStepScreenshotTool().execute(ctx=_ctx(), step_number=7, which="post")
    assert is_tool_failure(out)
    assert out == "get_step_screenshot failed: no screenshot for step 7"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        (SearchHistoryTool(), {"query": "x"}),
        (ReplayStepsTool(), {"start_step": 1}),
        (GetStepScreenshotTool(), {"step_number": 1}),
    ],
    ids=lambda v: getattr(v, "name", None),
)
async def test_missing_history_is_a_tool_failure(tool, kwargs):
    assert is_tool_failure(await tool.execute(ctx=None, **kwargs))
    assert is_tool_failure(await tool.execute(ctx=SimpleNamespace(data_engine=None), **kwargs))
