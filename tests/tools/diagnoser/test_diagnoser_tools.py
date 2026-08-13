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

from artemis.tools.diagnoser_submit_answer_tool import get_submit_answer_tool


def test_diagnoser_submit_answer_tool(artemis_context):
    tool = get_submit_answer_tool(artemis_context)

    # Execute the tool with some typical payload
    result = tool.invoke(
        {
            "analysis": "The problem is caused by a network timeout.",
            "actionable_steps": ["Check network connection", "Restart the server"],
        }
    )

    # Assert that the tool executes without raising errors and produces expected output structure
    assert result == "Answer submitted successfully."
    assert "error" not in result.lower()


def test_submit_answer_tool_subclass_and_registry():
    """Verify SubmitAnswerTool is an ArtemisTool subclass and registered in ToolRegistry."""
    from artemis.tools.base import ArtemisTool, ToolRegistry
    from artemis.tools.diagnoser_submit_answer_tool import (
        DiagnoserSubmitAnswerTool,
        SubmitAnswer,
        SubmitAnswerArgs,
        SubmitAnswerTool,
        submit_answer,
    )

    assert issubclass(SubmitAnswerTool, ArtemisTool)
    assert issubclass(SubmitAnswer, ArtemisTool)
    assert issubclass(DiagnoserSubmitAnswerTool, ArtemisTool)
    assert isinstance(submit_answer, ArtemisTool)
    assert isinstance(submit_answer, SubmitAnswerTool)

    assert submit_answer.name == "submit_answer"
    assert submit_answer.category == "custom"
    assert submit_answer.args_schema == SubmitAnswerArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("submit_answer")
    assert reg_tool is not None
    assert isinstance(reg_tool, SubmitAnswerTool)

    # GenAI FunctionDeclaration export
    declaration = submit_answer.to_genai_declaration()
    assert declaration.name == "submit_answer"
    assert "analysis" in declaration.parameters.properties
    assert "actionable_steps" in declaration.parameters.properties


def test_ask_diagnoser_tool_subclass_and_registry():
    """Verify AskDiagnoserTool is an ArtemisTool subclass and registered in ToolRegistry."""
    from artemis.tools.base import ArtemisTool, ToolRegistry
    from artemis.tools.diagnostic_tool import (
        AskDiagnoser,
        AskDiagnoserArgs,
        AskDiagnoserTool,
        DiagnosticTool,
        ask_diagnoser,
    )

    assert issubclass(AskDiagnoserTool, ArtemisTool)
    assert issubclass(AskDiagnoser, ArtemisTool)
    assert issubclass(DiagnosticTool, ArtemisTool)
    assert isinstance(ask_diagnoser, ArtemisTool)
    assert isinstance(ask_diagnoser, AskDiagnoserTool)

    assert ask_diagnoser.name == "ask_diagnoser"
    assert ask_diagnoser.category == "custom"
    assert ask_diagnoser.args_schema == AskDiagnoserArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("ask_diagnoser")
    assert reg_tool is not None
    assert isinstance(reg_tool, AskDiagnoserTool)

    # GenAI FunctionDeclaration export
    declaration = ask_diagnoser.to_genai_declaration()
    assert declaration.name == "ask_diagnoser"
    assert "query" in declaration.parameters.properties


def test_analyze_logs_tool_subclass_and_registry():
    """Verify AnalyzeLogsTool is an ArtemisTool subclass and registered in ToolRegistry."""
    from artemis.tools.base import ArtemisTool, ToolRegistry
    from artemis.tools.log_tool import (
        AnalyzeLogs,
        AnalyzeLogsArgs,
        AnalyzeLogsTool,
        LogTool,
        analyze_logs,
    )

    assert issubclass(AnalyzeLogsTool, ArtemisTool)
    assert issubclass(AnalyzeLogs, ArtemisTool)
    assert issubclass(LogTool, ArtemisTool)
    assert isinstance(analyze_logs, ArtemisTool)
    assert isinstance(analyze_logs, AnalyzeLogsTool)

    assert analyze_logs.name == "analyze_logs"
    assert analyze_logs.category == "custom"
    assert analyze_logs.args_schema == AnalyzeLogsArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("analyze_logs")
    assert reg_tool is not None
    assert isinstance(reg_tool, AnalyzeLogsTool)

    # GenAI FunctionDeclaration export
    declaration = analyze_logs.to_genai_declaration()
    assert declaration.name == "analyze_logs"
    assert "specific_query" in declaration.parameters.properties
