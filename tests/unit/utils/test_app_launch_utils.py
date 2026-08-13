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

from unittest.mock import AsyncMock, Mock, patch

from artemis.context import ArtemisContext
from artemis.utils.app_launch_utils import launch_app_with_retries
import pytest


@pytest.fixture
def mock_context():
    ctx = Mock(spec=ArtemisContext)
    ctx.device = Mock()
    ctx.device.mobile_platform = "android"
    ctx.device.device_id = "emulator-5554"
    ctx.data_engine = None
    return ctx


@pytest.mark.asyncio
@patch("artemis.utils.app_launch_utils.UnifiedMobileController")
@patch("artemis.utils.app_launch_utils.get_current_foreground_package_async")
async def test_launch_app_success_immediate(mock_get_foreground, mock_controller_cls, mock_context):
    """Test successful immediate app launch."""
    mock_controller = Mock()
    mock_controller.launch_app = AsyncMock(return_value=True)
    mock_controller_cls.return_value = mock_controller

    # App immediately in foreground
    mock_get_foreground.return_value = "com.google.android.youtube"

    success, error_msg = await launch_app_with_retries(
        mock_context,
        "com.google.android.youtube",
        max_retries=1,
        max_poll_seconds=2,
    )

    assert success is True
    assert error_msg is None
    mock_controller.launch_app.assert_called_once_with("com.google.android.youtube")


@pytest.mark.asyncio
@patch("artemis.utils.app_launch_utils.get_focused_task_package")
@patch("artemis.utils.app_launch_utils.UnifiedMobileController")
@patch("artemis.utils.app_launch_utils.get_current_foreground_package_async")
async def test_launch_app_success_permission_overlay(
    mock_get_foreground,
    mock_controller_cls,
    mock_get_focused_task,
    mock_context,
):
    """Test successful launch if overlayed by system permission controller and task is matching."""
    mock_controller = Mock()
    mock_controller.launch_app = AsyncMock(return_value=True)
    mock_controller_cls.return_value = mock_controller

    # System permission controller is focused foreground
    mock_get_foreground.return_value = "com.google.android.permissioncontroller"
    # Second layer check: Task stack shows com.google.android.youtube is focused
    mock_get_focused_task.return_value = "com.google.android.youtube"

    success, error_msg = await launch_app_with_retries(
        mock_context,
        "com.google.android.youtube",
        max_retries=1,
        max_poll_seconds=2,
    )

    assert success is True
    assert error_msg is None
    mock_controller.launch_app.assert_called_once_with("com.google.android.youtube")
    mock_get_focused_task.assert_called_once_with(mock_context)


@pytest.mark.asyncio
@patch("artemis.utils.app_launch_utils.get_focused_task_package")
@patch("artemis.utils.app_launch_utils.UnifiedMobileController")
@patch("artemis.utils.app_launch_utils.get_current_foreground_package_async")
async def test_launch_app_failure_permission_overlay_wrong_task(
    mock_get_foreground,
    mock_controller_cls,
    mock_get_focused_task,
    mock_context,
):
    """Test launch failure if overlayed by system permission controller but task does not match."""
    mock_controller = Mock()
    mock_controller.launch_app = AsyncMock(return_value=True)
    mock_controller.terminate_app = AsyncMock()
    mock_controller_cls.return_value = mock_controller

    # System permission controller is focused foreground
    mock_get_foreground.return_value = "com.google.android.permissioncontroller"
    # Second layer check: Task stack is some other app (e.g. com.android.settings)
    mock_get_focused_task.return_value = "com.android.settings"

    success, error_msg = await launch_app_with_retries(
        mock_context,
        "com.google.android.youtube",
        max_retries=1,
        max_poll_seconds=1,
    )

    assert success is False
    assert "Failed to launch com.google.android.youtube" in error_msg
    mock_get_focused_task.assert_called_once_with(mock_context)


@pytest.mark.asyncio
@patch("artemis.utils.app_launch_utils.UnifiedMobileController")
@patch("artemis.utils.app_launch_utils.get_current_foreground_package_async")
async def test_launch_app_failure_wrong_app(mock_get_foreground, mock_controller_cls, mock_context):
    """Test launch failure and retry when wrong app remains in foreground."""
    mock_controller = Mock()
    mock_controller.launch_app = AsyncMock(return_value=True)
    mock_controller.terminate_app = AsyncMock()
    mock_controller_cls.return_value = mock_controller

    # A different app (e.g., settings or home screen) is in foreground
    mock_get_foreground.return_value = "com.android.settings"

    success, error_msg = await launch_app_with_retries(
        mock_context,
        "com.google.android.youtube",
        max_retries=2,
        max_poll_seconds=1,
    )

    assert success is False
    assert "Failed to launch com.google.android.youtube" in error_msg

    # Should have attempted launching twice
    assert mock_controller.launch_app.call_count == 2
    # Should have attempted force-stopping once before retry
    assert mock_controller.terminate_app.call_count == 1
