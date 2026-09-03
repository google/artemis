"""Helper-tool routing in McpActionExecutor (Flash profile).

The Flash runner binds ``ask_explorer``, ``video_analyzer`` and the history
tools (``search_history`` / ``replay_steps`` / ``get_step_screenshot``) next to its device actions; the executor must
route them to the same implementations the Pro operator's LangChain tools
delegate to, as agent-side text tools (no screenshot capture, no device
action), and report their status explicitly instead of sniffing the text for
the word "Error".
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from artemis.mcp.action_executor import AGENT_TOOL_NAMES, McpActionExecutor
from artemis.tools.explorer_tool import ExplorerCandidate, ExplorerOutcome


def _make_executor(**kwargs):
    ctx = Mock()
    ctx.data_engine = None
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    actuator = Mock()
    actuator.controller = Mock()
    actuator.extensions.return_value = []
    return McpActionExecutor(ctx, actuator=actuator, **kwargs)


def _state():
    state = Mock()
    state.latest_screenshot = "/tmp/shot.jpg"
    state.indexed_points = []
    state.indexed_elements = []
    state.operator_raw_data = None
    return state


def test_helper_tools_are_not_device_actions():
    executor = _make_executor()
    assert {
        "ask_explorer",
        "video_analyzer",
        "search_history",
        "replay_steps",
        "get_step_screenshot",
    } <= AGENT_TOOL_NAMES
    assert not (AGENT_TOOL_NAMES & executor.action_tool_names)


# ---------------------------------------------------------------------------
# ask_explorer
# ---------------------------------------------------------------------------


def test_executor_agent_name_defaults_to_validator_and_is_configurable():
    assert _make_executor().agent_name == "validator"
    assert _make_executor(agent_name="flash").agent_name == "flash"


@pytest.mark.asyncio
async def test_ask_explorer_runs_the_pipeline_for_the_executor_agent():
    """The tier is resolved from the executor's agent name, never from the call."""
    executor = _make_executor(agent_name="flash")
    state = _state()
    outcome = ExplorerOutcome(
        candidates=[ExplorerCandidate(label="S1", coords=(500, 500), description="Send")]
    )
    with patch("artemis.tools.explorer_tool.locate", new=AsyncMock(return_value=outcome)) as locate:
        result = await executor.execute(
            "ask_explorer", {"query": "blue Send button", "context_feedback": "n/a"}, "tc", state
        )

    locate.assert_awaited_once_with(
        executor.ctx, state, "blue Send button", "n/a", agent_name="flash"
    )
    assert result.status == "success"
    assert "Explorer located 1 candidate(s) for 'blue Send button'" in result.text_summary
    assert "[1] 'Send' at normalized [500, 500]" in result.text_summary
    # Candidates were registered on the state for index-based actions.
    assert state.indexed_points == [[540, 1200]]
    assert state.indexed_elements[0]["index"] == 1
    assert result.screenshot_bytes is None
    assert result.ui_elements_text is None


@pytest.mark.asyncio
async def test_ask_explorer_clean_not_found_is_a_successful_answer():
    executor = _make_executor()
    outcome = ExplorerOutcome(message="Nothing like that is visible; the keyboard covers it.")
    with patch("artemis.tools.explorer_tool.locate", new=AsyncMock(return_value=outcome)):
        result = await executor.execute("ask_explorer", {"query": "gear icon"}, "tc", _state())
    assert result.status == "success"
    assert "Explorer could not locate 'gear icon'" in result.text_summary
    assert "keyboard covers it" in result.text_summary


@pytest.mark.asyncio
async def test_ask_explorer_run_failure_is_an_error_result():
    executor = _make_executor()
    outcome = ExplorerOutcome.failure("Explorer failed: model quota exhausted")
    with patch("artemis.tools.explorer_tool.locate", new=AsyncMock(return_value=outcome)):
        result = await executor.execute("ask_explorer", {"query": "gear icon"}, "tc", _state())
    assert result.status == "error"
    assert "Explorer could not run for 'gear icon'" in result.text_summary
    assert "model quota exhausted" in result.text_summary


@pytest.mark.asyncio
async def test_ask_explorer_accepts_the_legacy_task_description_alias():
    executor = _make_executor()
    outcome = ExplorerOutcome(message="not here")
    with patch("artemis.tools.explorer_tool.locate", new=AsyncMock(return_value=outcome)) as locate:
        result = await executor.execute(
            "ask_explorer", {"task_description": "gear icon top-right"}, "tc", _state()
        )
    assert locate.await_args.args[2] == "gear icon top-right"
    assert locate.await_args.args[3] == ""
    assert result.status == "success"


@pytest.mark.asyncio
async def test_ask_explorer_exception_is_contained():
    executor = _make_executor()
    with patch(
        "artemis.tools.explorer_tool.locate", new=AsyncMock(side_effect=RuntimeError("boom"))
    ):
        result = await executor.execute("ask_explorer", {"query": "x"}, "tc", _state())
    assert result.status == "error"
    assert result.text_summary == "Error executing ask_explorer: boom"


# ---------------------------------------------------------------------------
# Legacy MobileActionExecutor parity (same ask_explorer semantics)
# ---------------------------------------------------------------------------


def _make_legacy_executor(**kwargs):
    from artemis.agents.validator.tool_declarations import MobileActionExecutor

    ctx = Mock()
    ctx.data_engine = None
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    actuator = Mock()
    actuator.controller = Mock()
    return MobileActionExecutor(ctx, actuator=actuator, **kwargs)


@pytest.mark.asyncio
async def test_legacy_executor_ask_explorer_status_follows_the_outcome():
    executor = _make_legacy_executor()
    assert executor.agent_name == "validator"

    not_found = ExplorerOutcome(message="Not visible.")
    with patch("artemis.tools.explorer_tool.locate", new=AsyncMock(return_value=not_found)):
        # The pipeline functions are imported at module load in the legacy executor.
        with patch(
            "artemis.agents.validator.tool_declarations.locate",
            new=AsyncMock(return_value=not_found),
        ) as locate:
            result = await executor.execute(
                "ask_explorer", {"task_description": "gear icon"}, "tc", _state()
            )
    assert locate.await_args.args[2] == "gear icon"
    assert locate.await_args.kwargs["agent_name"] == "validator"
    assert result.status == "success"  # a clean not-found is an answer, not a failure

    failed = ExplorerOutcome.failure("Explorer failed: boom")
    with patch(
        "artemis.agents.validator.tool_declarations.locate", new=AsyncMock(return_value=failed)
    ):
        result = await executor.execute("ask_explorer", {"query": "gear icon"}, "tc", _state())
    assert result.status == "error"


@pytest.mark.asyncio
async def test_legacy_executor_agent_name_reaches_the_tier_resolver():
    executor = _make_legacy_executor(agent_name="flash")
    with patch(
        "artemis.agents.validator.tool_declarations.locate",
        new=AsyncMock(return_value=ExplorerOutcome(message="no")),
    ) as locate:
        await executor.execute("ask_explorer", {"query": "x"}, "tc", _state())
    assert locate.await_args.kwargs["agent_name"] == "flash"


# ---------------------------------------------------------------------------
# video_analyzer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_video_analyzer_routes_to_the_subagent_as_a_text_tool():
    executor = _make_executor()
    with patch("artemis.agents.video_analyzer.video_analyzer.VideoAnalyzer") as analyzer_cls:
        analyzer_cls.return_value.run = AsyncMock(
            return_value=("The ad finished at 12s; an Error dialog never appeared.", "success")
        )
        result = await executor.execute(
            "video_analyzer",
            {"time_description": "from 5s to 12s", "purpose": "what played"},
            "tc-video",
            None,
        )

    analyzer_cls.return_value.run.assert_awaited_once_with("from 5s to 12s", "what played")
    assert result.status == "success"  # explicit status, not text sniffing
    assert result.text_summary.startswith("The ad finished at 12s")
    assert result.screenshot_bytes is None
    assert result.ui_elements_text is None
    assert result.raw_result is None


@pytest.mark.asyncio
async def test_video_analyzer_failure_is_an_error_result():
    executor = _make_executor()
    with patch("artemis.agents.video_analyzer.video_analyzer.VideoAnalyzer") as analyzer_cls:
        analyzer_cls.return_value.run = AsyncMock(return_value=("no recording found", "failed"))
        result = await executor.execute(
            "video_analyzer", {"time_description": "from 0s to 3s", "purpose": "x"}, "tc", None
        )
    assert result.status == "error"
    assert result.text_summary == "Video analysis failed: no recording found"


@pytest.mark.asyncio
async def test_video_analyzer_exception_is_contained():
    executor = _make_executor()
    with patch("artemis.agents.video_analyzer.video_analyzer.VideoAnalyzer") as analyzer_cls:
        analyzer_cls.return_value.run = AsyncMock(side_effect=RuntimeError("boom"))
        result = await executor.execute(
            "video_analyzer", {"time_description": "from 0s to 3s", "purpose": "x"}, "tc", None
        )
    assert result.status == "error"
    assert "boom" in result.text_summary


# ---------------------------------------------------------------------------
# history tools (search_history / replay_steps / get_step_screenshot)
# ---------------------------------------------------------------------------


def _history_tool_double(return_value=None, side_effect=None):
    from artemis.tools.history import SearchHistoryArgs

    tool = Mock()
    tool.args_schema = SearchHistoryArgs
    tool.execute = AsyncMock(return_value=return_value, side_effect=side_effect)
    return tool


@pytest.mark.asyncio
async def test_history_tool_routes_to_the_shared_tool_with_text_result():
    executor = _make_executor()
    tool = _history_tool_double(return_value="- Step 3 (T+00:30): tap btn3 -> executed")
    with patch("artemis.mcp.action_executor.history_tool_by_name", return_value=tool) as by_name:
        result = await executor.execute(
            "search_history",
            {"query": "login Error timeout", "step_range": [1, 5], "unknown_arg": 1},
            "tc-search",
            None,
        )

    by_name.assert_called_once_with("search_history")
    tool.execute.assert_awaited_once_with(
        ctx=executor.ctx, query="login Error timeout", step_range=[1, 5]
    )
    # A lookup answer mentioning "Error" is still a successful lookup.
    assert result.status == "success"
    assert result.text_summary == "- Step 3 (T+00:30): tap btn3 -> executed"
    assert result.raw_result is None


@pytest.mark.asyncio
async def test_history_tool_forwards_multimodal_blocks():
    executor = _make_executor()
    blocks = [
        {"type": "text", "text": "Screenshot of step 4 (pre-action) is attached."},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
    ]
    tool = _history_tool_double(return_value=blocks)
    with patch("artemis.mcp.action_executor.history_tool_by_name", return_value=tool):
        result = await executor.execute("get_step_screenshot", {"query": "unused"}, "tc", None)
    assert result.status == "success"
    assert result.text_summary == "Screenshot of step 4 (pre-action) is attached."
    assert result.raw_result == blocks


@pytest.mark.asyncio
async def test_history_tool_exception_is_contained():
    executor = _make_executor()
    tool = _history_tool_double(side_effect=RuntimeError("db gone"))
    with patch("artemis.mcp.action_executor.history_tool_by_name", return_value=tool):
        result = await executor.execute("replay_steps", {"query": "x"}, "tc", None)
    assert result.status == "success"  # contained: the answer explains the failure
    assert result.text_summary == "replay_steps failed: db gone"


@pytest.mark.asyncio
async def test_history_tools_dispatch_to_the_real_shared_instances():
    """No per-tool branch: every history tool name resolves through the package."""
    executor = _make_executor()  # ctx.data_engine is None -> tools degrade to text
    for name in ("search_history", "replay_steps", "get_step_screenshot"):
        result = await executor.execute(
            name, {"query": "x", "start_step": 1, "step_number": 1}, "tc", None
        )
        assert result.status == "success"
        assert (
            "no active execution history" in result.text_summary
            or "no execution history" in result.text_summary
        )
