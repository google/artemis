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

"""The Pro operator's context base on a provider without usage metadata.

The transcript ledger's start gate and soft/hard thresholds read the
operator's own measured prompt size; when the provider reports no usage the
operator records a provider-less estimate instead (Flash already does), so
the thresholds still see a base. A measured call also hands the ledger the
messages it was measured on, for the chars-per-token calibration.
"""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.agents.operator.operator import OperatorNode
from artemis.config.agent import MemoryTranscriptConfig
from artemis.context import ArtemisContext
from artemis.memory.step_memory import StepMemoryService
from artemis.memory.transcript import TranscriptLedger, estimate_prompt_tokens

SCREENSHOT_B64 = base64.b64encode(b"fake-jpeg-bytes").decode("utf-8")


def _ctx():
    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = None
    ctx.actuator = None
    ctx.data_engine = None
    ctx.step_memory = StepMemoryService(ctx=None)
    ctx.transcript_ledger = None
    return ctx


def _state():
    state = MagicMock()
    state.subagent_calls = []
    state.initial_goal = "Estimate goal"
    state.operator_feedback = None
    state.injected_instruction = None
    state.operator_tool_limit_exceeded = False
    state.structured_decisions = None
    state.last_execution_result = None
    state.current_step_id = None
    state.latest_ui_hierarchy = []
    state.operator_raw_data = {
        "screenshot_b64": SCREENSHOT_B64,
        "xml_hierarchy": [],
        "ocr_results": None,
        "width": 1080,
        "height": 2400,
    }
    return state


def _llm(captured: list, usage_metadata=None):
    mock_llm = MagicMock()
    response = MagicMock()
    response.tool_calls = []
    response.content = "no action this turn"
    # usage_from_message only trusts a non-empty dict; a MagicMock attribute
    # (the default) reads as "no usage reported".
    if usage_metadata is not None:
        response.usage_metadata = usage_metadata

    async def ainvoke(*args, **kwargs):
        captured.append(list(args[0]))
        return response

    mock_llm.ainvoke = AsyncMock(side_effect=ainvoke)
    mock_llm.bind_tools.return_value = mock_llm
    return mock_llm


@pytest.mark.asyncio
async def test_operator_records_an_estimate_when_the_provider_reports_no_usage():
    ctx = _ctx()
    captured: list = []
    with patch("artemis.agents.operator.operator.get_llm", return_value=_llm(captured)):
        node = OperatorNode(ctx, transcript_config=MemoryTranscriptConfig(enabled=True))
        await node(_state())

    ledger = ctx.transcript_ledger
    assert isinstance(ledger, TranscriptLedger)
    assert captured, "the operator made no model call"
    assert ledger.last_prompt_tokens == estimate_prompt_tokens(captured[-1])
    assert ledger.last_prompt_tokens > 0
    # No measurement, no calibration.
    assert ledger.chars_per_token == 4.0


@pytest.mark.asyncio
async def test_operator_measured_usage_calibrates_the_ledger_ratio():
    ctx = _ctx()
    captured: list = []
    llm = _llm(
        captured,
        usage_metadata={"input_tokens": 50_000, "output_tokens": 10, "total_tokens": 50_010},
    )
    with patch("artemis.agents.operator.operator.get_llm", return_value=llm):
        node = OperatorNode(ctx, transcript_config=MemoryTranscriptConfig(enabled=True))
        await node(_state())

    ledger = ctx.transcript_ledger
    assert ledger.last_prompt_tokens == 50_000
    # The prompt is a few thousand chars of English plus one image; measured
    # at 50k tokens the ratio clamps to the lower bound — proof the messages
    # reached the calibration, not just the token figure.
    assert ledger.chars_per_token == 1.0
