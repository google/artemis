# Copyright 2025-2026 Minitap, Inc.
# Modifications Copyright 2026 Google LLC
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
#
# Originally from mobile-use (https://github.com/minitap-ai/mobile-use).
# See third_party/mobile_use/METADATA for the upstream source and local modifications.

"""Pytest fixtures for agent node tests (upstream ``agents/outputter/test_outputter.py``).

Test-only: imports pytest, so it must not be imported by runtime code.
"""

from unittest.mock import Mock

import pytest


class DummyState:
    def __init__(self, messages, initial_goal, operator_raw_data=None):
        self.messages = messages
        self.initial_goal = initial_goal
        self.operator_raw_data = operator_raw_data


@pytest.fixture
def mock_context():
    """Create a properly mocked context with all required fields."""
    from artemis.config import LLM
    from artemis.context import ArtemisContext

    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = {
        "planner": LLM(provider="openai", model="gpt-5-nano"),
        "operator": LLM(provider="openai", model="gpt-5-nano"),
        "validator": LLM(provider="openai", model="gpt-5-nano"),
    }
    ctx.device = Mock()
    ctx.data_engine = None
    return ctx


@pytest.fixture
def mock_state():
    """Create a mock state with test data."""
    return DummyState(
        messages=[],
        initial_goal="Find a green product on my website",
    )
