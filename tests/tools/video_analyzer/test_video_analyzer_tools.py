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
from unittest.mock import MagicMock, patch

import pytest
from artemis.agents.video_analyzer.video_analyzer import VideoAnalyzer


@pytest.mark.asyncio
async def test_video_analyzer_tools(artemis_context, mock_state, inputs_dir):
    artemis_context.device.mobile_platform = "android"
    artemis_context.adb_client = artemis_context.ui_adb_client

    agent = VideoAnalyzer(ctx=artemis_context)

    agent.local_files_to_cleanup = set()
    agent.local_dirs_to_cleanup = set()
    agent.cloud_files_to_cleanup = set()
    agent.blackboard_entries = []
    agent.sub_system_prompt = "You are a sub agent"
    agent.audio_system_prompt = "You are an audio sub agent"
    agent.model_name = "gemini-3.7-flash"

    # We must provide a valid FunctionDeclaration for the tool otherwise Pydantic will complain
    from google.genai import types

    agent.submit_answer_tool = types.FunctionDeclaration(
        name="submit_answer",
        description=("Submit the final findings for the requested segment analysis."),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "summary": types.Schema(type=types.Type.STRING, description="summary"),
                "analysis": types.Schema(type=types.Type.STRING, description="analysis"),
                "timeline_events": types.Schema(
                    type=types.Type.ARRAY,
                    items=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "start_time": types.Schema(type=types.Type.NUMBER),
                            "end_time": types.Schema(type=types.Type.NUMBER),
                            "transcription": types.Schema(type=types.Type.STRING),
                            "confidence_score": types.Schema(type=types.Type.NUMBER),
                            "verification_timestamp_secs": types.Schema(type=types.Type.NUMBER),
                        },
                    ),
                ),
            },
        ),
    )

    import google.genai as genai
    from artemis.config import settings

    agent.client = genai.Client(
        api_key=settings.GOOGLE_API_KEY.get_secret_value() if settings.GOOGLE_API_KEY else None
    )
    artemis_context._genai_client = agent.client

    mock_session = MagicMock()
    mock_session.local_video_path = Path(inputs_dir) / "recording.mp4"
    mock_session.start_time = 0.0
    mock_session.data_engine_start_time = None
    mock_session.process = MagicMock()
    mock_session.process.returncode = None
    mock_session.android_video_segments = []
    mock_session.android_segment_index = 0

    from unittest.mock import AsyncMock

    sample_video = Path(inputs_dir) / "recording.mp4"
    mock_driver = MagicMock()
    mock_driver.stop_video_recording = AsyncMock(return_value=sample_video)
    artemis_context._active_driver = mock_driver

    with patch(
        "artemis.controllers.android_controller.get_active_session",
        return_value=mock_session,
    ):
        result = await agent.exec_extract_segment_metadata(start_time=0.0, end_time=1.0)
        assert isinstance(result, str)
        assert len(result) > 0

        # Test sub-agent with extremely short audio query to not waste resources and time
        audio_result = await agent.exec_analyze_audio_only(
            start_time=0.0,
            end_time=1.0,
            specific_query="Reply with a short summary only.",
        )
        assert isinstance(audio_result, str)
