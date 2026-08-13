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

from google import genai
from google.genai import types
from google.genai.errors import APIError
import pytest


@pytest.fixture
def client():
    """Initializes the Gemini client. Requires GEMINI_API_KEY in environment."""
    try:
        return genai.Client()
    except Exception as e:
        pytest.skip(f"Could not initialize genai.Client: {e}")


def test_gemini_role_tool_fails(client):
    """Confirm that using role='tool' results in a 400 APIError from Gemini."""
    contents = [
        types.Content(
            role="tool",
            parts=[types.Part.from_text(text="This is a test payload for tool execution.")],
        )
    ]

    with pytest.raises(APIError) as exc_info:
        client.models.generate_content(
            model="gemini-3.6-flash",
            contents=contents,
        )

    # Assert it failed specifically due to the invalid role string
    error_msg = str(exc_info.value)
    assert "Invalid role string" in error_msg or "400" in error_msg


def test_gemini_role_user_succeeds(client):
    """Confirm that using role='user' (which is the correct fix) succeeds."""
    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(text="Acknowledge this message with 'ok' and nothing else.")
            ],
        )
    ]

    # This should not raise any exceptions
    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=contents,
    )

    assert response.text is not None
