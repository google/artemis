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

"""Global pytest fixtures for ARTEMIS test suite."""

import pytest
from artemis.core.context import ExecutionContext
from artemis.drivers.mock.mock_driver import MockDeviceDriver


@pytest.fixture
def mock_driver():
    """Provides an isolated MockDeviceDriver instance."""
    return MockDeviceDriver(device_id="fixture-mock-device", width=1080, height=2400)


@pytest.fixture
def test_context():
    """Provides a fresh ExecutionContext."""
    return ExecutionContext(task_goal="Test sample task objective", device_id="fixture-mock-device")
