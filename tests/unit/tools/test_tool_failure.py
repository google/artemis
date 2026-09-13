"""Helper-tool failures are structural (``ToolFailure`` / ``ToolMessage.status``),
never inferred from the words of the text."""

from unittest.mock import Mock

from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
import pytest

from artemis.core.tool_failure import ToolFailure, is_tool_failure
from artemis.data_engine import engine as engine_mod
from artemis.tools.tool_wrapper import invoke_tool_with_injection
from artemis.utils.notes import format_list_notes_failure, format_read_note_failure


def test_tool_failure_is_a_plain_string_to_every_text_consumer():
    failure = ToolFailure("Failed to save note plan.md: disk full")
    assert isinstance(failure, str)
    assert failure == "Failed to save note plan.md: disk full"
    assert f"{failure}" == "Failed to save note plan.md: disk full"
    assert failure.startswith("Failed")


def test_is_tool_failure_reads_the_structure_not_the_words():
    assert is_tool_failure(ToolFailure("Error: note not found"))
    assert is_tool_failure(ToolFailure("plain wording still counts"))
    # Free-form text may legitimately start with "Error" or "Failed".
    assert not is_tool_failure("Error 404 was typed into the search box")
    assert not is_tool_failure("Failed login dialog is visible")
    assert not is_tool_failure("Successfully saved note")
    assert not is_tool_failure(None)


def test_is_tool_failure_honours_the_tool_message_status():
    assert is_tool_failure(ToolMessage(tool_call_id="t", content="anything", status="error"))
    assert not is_tool_failure(
        ToolMessage(tool_call_id="t", content="Error-looking text", status="success")
    )


def test_is_tool_failure_finds_a_failure_text_block():
    assert is_tool_failure([{"type": "text", "text": ToolFailure("boom")}])
    assert is_tool_failure([ToolFailure("boom"), {"type": "image_url", "image_url": {}}])
    assert not is_tool_failure([{"type": "text", "text": "Error: fine"}])


def test_note_failure_formatters_report_structurally():
    assert is_tool_failure(format_read_note_failure("plan", "file not found"))
    assert is_tool_failure(format_read_note_failure("plan", "permission denied"))
    assert is_tool_failure(format_list_notes_failure("boom"))


def test_tool_message_content_coerces_a_tool_failure_to_plain_str():
    """Pydantic drops the ``str`` subclass: the failure must be read from the raw
    tool return *before* the message is built, and carried as ``status``."""
    raw = ToolFailure("boom")
    assert is_tool_failure(raw)

    msg = ToolMessage(tool_call_id="t", content=raw)
    assert type(msg.content) is str
    assert is_tool_failure(msg.content) is False
    assert is_tool_failure(msg) is False  # default status is "success"

    explicit = ToolMessage(
        tool_call_id="t", content=raw, status="error" if is_tool_failure(raw) else "success"
    )
    assert is_tool_failure(explicit)


# --- invoke_tool_with_injection: the trace span follows the structure ---------------


def _text_tool(name: str, result):
    async def _run(query: str) -> str:
        return result

    return StructuredTool.from_function(coroutine=_run, name=name, description=name)


def _stub_engine(monkeypatch):
    engine = Mock()
    engine.current_step_id = "step-1"
    monkeypatch.setattr(engine_mod, "_CURRENT_DATA_ENGINE", engine, raising=False)
    return engine


def _final_trace_call(engine):
    calls = [c for c in engine.record_trace.call_args_list if c.kwargs.get("status") != "running"]
    assert len(calls) == 1
    return calls[0].kwargs


@pytest.mark.asyncio
async def test_invoke_tool_with_injection_traces_a_tool_failure_as_failed(monkeypatch):
    engine = _stub_engine(monkeypatch)
    tool = _text_tool("read_note", ToolFailure("Failed to read note plan.md: not found"))

    result = await invoke_tool_with_injection(tool, {"query": "plan.md"}, "tc-1")

    assert is_tool_failure(result)
    span = _final_trace_call(engine)
    assert span["status"] == "failed"
    assert span["name"] == "read_note"
    assert span["payload"]["error"] == "Failed to read note plan.md: not found"
    assert "result" not in span["payload"]


@pytest.mark.asyncio
async def test_invoke_tool_with_injection_traces_plain_error_text_as_success(monkeypatch):
    engine = _stub_engine(monkeypatch)
    tool = _text_tool("search_history", "Error: 3 matches for the typed search term")

    result = await invoke_tool_with_injection(tool, {"query": "Error:"}, "tc-2")

    assert not is_tool_failure(result)
    span = _final_trace_call(engine)
    assert span["status"] == "success"
    assert span["payload"]["result"] == "Error: 3 matches for the typed search term"
    assert "error" not in span["payload"]
