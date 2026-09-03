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

from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from artemis.context import ArtemisContext
from artemis.tools.committee_tool import get_ask_committee_tool


class TestCommitteeTool(unittest.IsolatedAsyncioTestCase):
    async def test_ask_committee_tool_with_directive(self):
        # Create a temp directory for testing
        temp_dir = tempfile.mkdtemp()

        try:
            notes_dir = Path(temp_dir) / "notes"
            notes_dir.mkdir(parents=True, exist_ok=True)

            # Create a mock screenshot file
            mock_screenshot_path = Path(temp_dir) / "mock_screenshot.jpg"
            mock_screenshot_path.write_bytes(b"fake image bytes")

            # Create a mock failed plans history file
            mock_failed_plans_history_path = notes_dir / "failed_plans_history.md"
            mock_failed_plans_history_path.write_text(
                "## Failed Plan Attempt #1\n### Plan:\n- [ ] Step 1\n###"
                " Failure Reason:\nTimeout waiting for element",
                encoding="utf-8",
            )
            # Mock context and state
            mock_ctx = MagicMock(spec=ArtemisContext)
            mock_ctx.device = MagicMock()
            mock_ctx.device.device_width = 1080
            mock_ctx.device.device_height = 2400
            mock_ctx.data_engine = MagicMock()
            mock_ctx.data_engine.base_dir = temp_dir
            mock_ctx.data_engine.get_agent_friendly_steps.return_value = [
                {"pre_image_name": "test_image", "step_number": 1}
            ]

            mock_state = MagicMock()
            mock_state.initial_goal = "Test Goal"
            mock_state.messages = []
            mock_state.latest_ui_hierarchy = None
            mock_state.latest_screenshot = str(mock_screenshot_path)
            mock_state.focused_app_info = None
            mock_state.device_date = None
            mock_state.structured_decisions = None
            mock_state.complete_subgoals_by_ids = []

            # Mock LLM responses for the members
            mock_llm_pl = MagicMock()
            mock_llm_pl.bind_tools.return_value = mock_llm_pl

            m1 = MagicMock(content="Op: I think we should tap.")
            m1.tool_calls = []
            m2 = MagicMock(content="Op: I agree with Exp.")
            m2.tool_calls = []
            m3 = MagicMock(content="Op: Final conclusion: Tap at [50, 50]")
            m3.tool_calls = []

            mock_llm_pl.ainvoke = AsyncMock(side_effect=[m1, m2, m3])

            mock_llm_diag = MagicMock()
            mock_llm_diag.bind_tools.return_value = mock_llm_diag
            m_diag = MagicMock(content="Diag: I see an error.")
            m_diag.tool_calls = []
            mock_llm_diag.ainvoke = AsyncMock(return_value=m_diag)

            # Mock History Analyzer to call a tool first
            mock_llm_hist = MagicMock()
            mock_llm_hist.bind_tools.return_value = mock_llm_hist

            mock_response_tool_call = MagicMock()
            mock_response_tool_call.tool_calls = [
                {
                    "name": "get_step_details",
                    "args": {"start_step": 1, "end_step": 1},
                    "id": "call_1",
                }
            ]
            mock_response_tool_call.content = ""

            mock_response_final = MagicMock()
            mock_response_final.tool_calls = []
            mock_response_final.content = "Hist: I found step 1 details."

            m_hist_agree = MagicMock(content="Hist: Still agree.")
            m_hist_agree.tool_calls = []

            mock_llm_hist.ainvoke = AsyncMock(
                side_effect=[
                    mock_response_tool_call,
                    mock_response_final,
                    # For second round
                    m_hist_agree,
                ]
            )

            def mock_get_llm(ctx, name, temperature=None):
                if name == "planner_avatar":
                    return mock_llm_pl
                elif name == "diagnoser_expert":
                    return mock_llm_diag
                elif name == "history_analyzer_expert":
                    return mock_llm_hist
                return MagicMock()

            # Patch get_llm and the actual tool functions to avoid real calls
            with (
                patch(
                    "artemis.tools.committee_tool.get_llm",
                    side_effect=mock_get_llm,
                ),
                patch(
                    "artemis.tools.committee_tool.trace_langchain_tool",
                    side_effect=lambda t, ctx: t,
                ),
            ):
                tool = get_ask_committee_tool(mock_ctx)

                # Invoke tool with avatar_directive
                from artemis.data_engine.trace import CURRENT_TRACE_ID

                token = CURRENT_TRACE_ID.set("test_trace_id")
                try:
                    result = await tool.ainvoke(
                        {
                            "avatar_directive": "Advocate for paying",
                            "state": mock_state,
                        }
                    )
                finally:
                    CURRENT_TRACE_ID.reset(token)

                # Verify result
                self.assertIn("Final conclusion: Tap at [50, 50]", result)

                # Verify file was created and contains content
                notes_dir = Path(temp_dir) / "notes"
                blackboard_files = list(notes_dir.glob("blackboard_*.md"))
                self.assertTrue(len(blackboard_files) > 0, "No blackboard file created")
                blackboard_path = blackboard_files[0]
                content = blackboard_path.read_text(encoding="utf-8")
                self.assertIn("Advocate for paying", content)  # Check directive is in blackboard
                self.assertIn("Hist: I found step 1 details.", content)
                self.assertIn(
                    "Timeout waiting for element", content
                )  # Check failed history is in blackboard
                self.assertIn("Test Goal", content)  # Check initial goal is in blackboard

        finally:
            # Clean up temp directory
            shutil.rmtree(temp_dir)

    def test_ask_committee_tool_subclass(self):
        """Verify AskCommitteeTool is a subclass of ArtemisTool."""
        from artemis.tools.base import ArtemisTool
        from artemis.tools.committee_tool import (
            AskCommittee,
            AskCommitteeArgs,
            AskCommitteeTool,
            AskCommitteeToolAlias,
            ask_committee,
            ask_committee_wrapper,
            get_ask_committee_tool,
        )

        self.assertTrue(issubclass(AskCommitteeTool, ArtemisTool))
        self.assertTrue(issubclass(AskCommittee, ArtemisTool))
        self.assertTrue(issubclass(AskCommitteeToolAlias, ArtemisTool))
        self.assertIsInstance(ask_committee, ArtemisTool)
        self.assertIsInstance(ask_committee, AskCommitteeTool)

        self.assertEqual(ask_committee.name, "ask_committee")
        self.assertEqual(ask_committee.category, "custom")
        self.assertEqual(ask_committee.args_schema, AskCommitteeArgs)

        # GenAI FunctionDeclaration export
        declaration = ask_committee.to_genai_declaration()
        self.assertEqual(declaration.name, "ask_committee")
        self.assertIn("avatar_directive", declaration.parameters.properties)

        # Wrapper check
        self.assertIsNotNone(ask_committee_wrapper)
        self.assertEqual(ask_committee_wrapper.tool_fn_getter, get_ask_committee_tool)

    async def test_ask_committee_no_ctx(self):
        """Verify executing without ctx returns an error message."""
        from artemis.tools.committee_tool import ask_committee

        result = await ask_committee.execute(avatar_directive="Test")
        self.assertEqual(result, "Error: ArtemisContext is required for ask_committee.")


if __name__ == "__main__":
    unittest.main()
