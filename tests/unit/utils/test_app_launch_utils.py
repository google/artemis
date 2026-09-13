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
from artemis.utils.app_launch_utils import (
    ForegroundTask,
    _handle_initial_app_launch,
    get_foreground_task,
    launch_app_with_retries,
    parse_foreground_task,
)
import pytest

SETTINGS = "com.android.settings"
SETTINGS_SEARCH = "com.google.android.settings.intelligence"
LAUNCHER = "com.google.android.apps.nexuslauncher"
YOUTUBE = "com.google.android.youtube"

# Trimmed from a real Pixel 10 ``dumpsys activity activities`` right after
# ``monkey -p com.android.settings -c android.intent.category.LAUNCHER 1`` re-opened a
# Settings task whose previous run had left the search activity (a different package)
# resumed on top. The focused window is settings.intelligence; the task is Settings'.
DUMP_SETTINGS_TASK_WITH_SEARCH_ON_TOP = """\
ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)
Display #0 (activities from top to bottom):
  * Task{97d2927 #121 type=standard A=1000:com.android.settings.root U=0 visible=true visibleRequested=true mode=fullscreen translucent=false sz=2}
    mLastPausedActivity: ActivityRecord{248426470 u0 com.android.settings/.Settings t121}
    isSleeping=false
    topResumedActivity=ActivityRecord{55960290 u0 com.google.android.settings.intelligence/.modules.search.SearchActivity t121}
    * Hist  #1: ActivityRecord{55960290 u0 com.google.android.settings.intelligence/.modules.search.SearchActivity t121}
      packageName=com.google.android.settings.intelligence processName=com.google.android.settings.intelligence
      launchedFromUid=1000 launchedFromPackage=com.android.settings launchedFromFeature=null userId=0
      rootOfTask=false task=Task{97d2927 #121 type=standard A=1000:com.android.settings.root}
      taskAffinity=10164:com.google.android.settings.intelligence
      state=RESUMED delayedResume=false finishing=false
    * Hist  #0: ActivityRecord{248426470 u0 com.android.settings/.Settings t121}
      packageName=com.android.settings processName=com.android.settings
      rootOfTask=true task=Task{97d2927 #121 type=standard A=1000:com.android.settings.root}
      taskAffinity=1000:com.android.settings.root
      state=STOPPED delayedResume=false finishing=false
  * Task{9580295 #7 type=home U=0 visible=false visibleRequested=false mode=fullscreen translucent=true sz=1}
    * Task{a3c724c #8 type=home I=com.google.android.apps.nexuslauncher/.NexusLauncherActivity U=0 rootTaskId=7 visible=false visibleRequested=false mode=fullscreen translucent=true sz=2}
      * Hist  #1: ActivityRecord{230175776 u0 com.google.android.apps.nexuslauncher/.NexusLauncherActivity t8}
        state=STOPPED delayedResume=false finishing=false
  * Task{ebe93a1 #108 type=standard A=10194:com.google.android.apps.maps U=0 visible=false visibleRequested=false mode=fullscreen translucent=true sz=1}
    * Hist  #0: ActivityRecord{210087944 u0 com.google.android.apps.maps/com.google.android.maps.MapsActivity t108}
  ResumedActivity: ActivityRecord{55960290 u0 com.google.android.settings.intelligence/.modules.search.SearchActivity t121}
  mFocusedApp=ActivityRecord{55960290 u0 com.google.android.settings.intelligence/.modules.search.SearchActivity t121}
    WindowContainer hierarchy:
      * Task{97d2927 #121 type=standard A=1000:com.android.settings.root U=0 visible=true visibleRequested=true mode=fullscreen translucent=false sz=2}
        * ActivityRecord{55960290 u0 com.google.android.settings.intelligence/.modules.search.SearchActivity t121}
        * ActivityRecord{248426470 u0 com.android.settings/.Settings t121}
"""

# Same device after KEYCODE_HOME: the launcher's (nested) home task is resumed.
DUMP_HOME = """\
ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)
Display #0 (activities from top to bottom):
  * Task{9580295 #7 type=home U=0 visible=true visibleRequested=true mode=fullscreen translucent=false sz=1}
    * Task{a3c724c #8 type=home I=com.google.android.apps.nexuslauncher/.NexusLauncherActivity U=0 rootTaskId=7 visible=true visibleRequested=true mode=fullscreen translucent=false sz=2}
      topResumedActivity=ActivityRecord{230175776 u0 com.google.android.apps.nexuslauncher/.NexusLauncherActivity t8}
      * Hist  #1: ActivityRecord{230175776 u0 com.google.android.apps.nexuslauncher/.NexusLauncherActivity t8}
        state=RESUMED delayedResume=false finishing=false
  * Task{97d2927 #121 type=standard A=1000:com.android.settings.root U=0 visible=false visibleRequested=false mode=fullscreen translucent=true sz=2}
    * Hist  #1: ActivityRecord{55960290 u0 com.google.android.settings.intelligence/.modules.search.SearchActivity t121}
    * Hist  #0: ActivityRecord{248426470 u0 com.android.settings/.Settings t121}
  ResumedActivity: ActivityRecord{230175776 u0 com.google.android.apps.nexuslauncher/.NexusLauncherActivity t8}
"""

# Search activity started directly with ``am start -n``: it lands in its own task, so
# Settings is *not* the foreground app even though its task still exists underneath.
DUMP_SEARCH_IN_OWN_TASK = """\
Display #0 (activities from top to bottom):
  * Task{7f6102 #120 type=standard A=10164:com.google.android.settings.intelligence U=0 visible=true visibleRequested=true mode=fullscreen translucent=false sz=1}
    topResumedActivity=ActivityRecord{53985869 u0 com.google.android.settings.intelligence/.modules.search.SearchActivity t120}
    * Hist  #0: ActivityRecord{53985869 u0 com.google.android.settings.intelligence/.modules.search.SearchActivity t120}
  * Task{c38138b #119 type=standard A=1000:com.android.settings.root U=0 visible=false visibleRequested=false mode=fullscreen translucent=true sz=3}
    * Hist  #2: ActivityRecord{24290640 u0 com.android.settings/.SubSettings t119}
    * Hist  #1: ActivityRecord{199753729 u0 com.android.settings/.SubSettings t119}
    * Hist  #0: ActivityRecord{33224026 u0 com.android.settings/.Settings t119}
  ResumedActivity: ActivityRecord{53985869 u0 com.google.android.settings.intelligence/.modules.search.SearchActivity t120}
"""

# Mid-transition: nothing is resumed yet, the visible task must be used instead.
DUMP_NO_RESUMED = """\
Display #0 (activities from top to bottom):
  * Task{97d2927 #121 type=standard A=1000:com.android.settings.root U=0 visible=true visibleRequested=true mode=fullscreen translucent=false sz=1}
    * Hist  #0: ActivityRecord{248426470 u0 com.android.settings/.Settings t121}
  * Task{ebe93a1 #108 type=standard A=10194:com.google.android.apps.maps U=0 visible=false visibleRequested=false mode=fullscreen translucent=true sz=1}
    * Hist  #0: ActivityRecord{210087944 u0 com.google.android.apps.maps/com.google.android.maps.MapsActivity t108}
"""


@pytest.fixture
def mock_context():
    ctx = Mock(spec=ArtemisContext)
    ctx.device = Mock()
    ctx.device.mobile_platform = "android"
    ctx.device.device_id = "emulator-5554"
    ctx.data_engine = None
    return ctx


def _controller(mock_controller_cls):
    mock_controller = Mock()
    mock_controller.launch_app = AsyncMock(return_value=True)
    mock_controller.terminate_app = AsyncMock()
    mock_controller_cls.return_value = mock_controller
    return mock_controller


# ---------------------------------------------------------------------------
# parse_foreground_task / ForegroundTask
# ---------------------------------------------------------------------------


def test_parse_settings_task_with_foreign_activity_on_top():
    task = parse_foreground_task(DUMP_SETTINGS_TASK_WITH_SEARCH_ON_TOP)

    assert task is not None
    assert task.task_id == 121
    assert task.affinity == "com.android.settings.root"
    assert task.resumed_package == SETTINGS_SEARCH
    assert task.resumed_component == f"{SETTINGS_SEARCH}/.modules.search.SearchActivity"
    assert task.base_package == SETTINGS
    assert task.packages == frozenset({SETTINGS, SETTINGS_SEARCH})

    assert task.owns(SETTINGS)  # base activity of the top task
    assert task.owns(SETTINGS_SEARCH)  # resumed activity itself
    assert not task.owns(LAUNCHER)
    assert not task.owns("com.google.android.apps.maps")  # present, but not foreground


def test_parse_home_screen_uses_nested_home_task():
    task = parse_foreground_task(DUMP_HOME)

    assert task is not None
    assert task.task_id == 8
    assert task.affinity is None
    assert task.base_package == LAUNCHER
    assert task.resumed_package == LAUNCHER
    assert task.owns(LAUNCHER)
    assert not task.owns(SETTINGS)
    assert not task.owns(SETTINGS_SEARCH)


def test_parse_search_started_in_its_own_task_does_not_count_as_settings():
    task = parse_foreground_task(DUMP_SEARCH_IN_OWN_TASK)

    assert task is not None
    assert task.task_id == 120
    assert task.base_package == SETTINGS_SEARCH
    assert task.owns(SETTINGS_SEARCH)
    assert not task.owns(SETTINGS)


def test_parse_falls_back_to_visible_task_when_nothing_is_resumed():
    task = parse_foreground_task(DUMP_NO_RESUMED)

    assert task is not None
    assert task.task_id == 121
    assert task.resumed_package is None
    assert task.base_package == SETTINGS
    assert task.owns(SETTINGS)
    assert not task.owns("com.google.android.apps.maps")


def test_parse_returns_none_without_task_information():
    assert parse_foreground_task("") is None
    assert parse_foreground_task("ACTIVITY MANAGER ACTIVITIES\nnothing here\n") is None


def test_describe_names_task_base_and_resumed_activity():
    task = parse_foreground_task(DUMP_SETTINGS_TASK_WITH_SEARCH_ON_TOP)

    description = task.describe()

    assert "task #121" in description
    assert f"base={SETTINGS}" in description
    assert f"resumed={SETTINGS_SEARCH}/.modules.search.SearchActivity" in description
    assert "affinity=com.android.settings.root" in description


def test_describe_lists_other_member_packages():
    task = ForegroundTask(
        task_id=5,
        resumed_component="a.b/.Dialog",
        resumed_package="a.b",
        base_package="c.d",
        packages=frozenset({"a.b", "c.d", "e.f"}),
    )

    assert task.describe() == "task #5 (base=c.d, resumed=a.b/.Dialog, also=e.f)"


@patch("artemis.utils.app_launch_utils.get_adb_device")
def test_get_foreground_task_reads_dumpsys(mock_get_device, mock_context):
    device = Mock()
    device.shell.return_value = DUMP_SETTINGS_TASK_WITH_SEARCH_ON_TOP
    mock_get_device.return_value = device

    task = get_foreground_task(mock_context)

    # The dump is pre-filtered on the device; the parser only needs task
    # headers, history entries and the resumed-activity lines.
    device.shell.assert_called_once()
    command = device.shell.call_args.args[0]
    assert command.startswith("dumpsys activity activities | grep -E ")
    assert task is not None and task.base_package == SETTINGS


@patch("artemis.utils.app_launch_utils.get_adb_device")
def test_get_foreground_task_falls_back_to_the_full_dump_without_grep(
    mock_get_device, mock_context
):
    device = Mock()
    device.shell.side_effect = [
        "/system/bin/sh: grep: not found",
        DUMP_SETTINGS_TASK_WITH_SEARCH_ON_TOP,
    ]
    mock_get_device.return_value = device

    task = get_foreground_task(mock_context)

    assert [c.args[0] for c in device.shell.call_args_list][-1] == "dumpsys activity activities"
    assert task is not None and task.base_package == SETTINGS


def test_task_header_accepts_android_11_taskrecord_form():
    from artemis.utils.app_launch_utils import parse_foreground_task

    dump = "\n".join(
        [
            "    * TaskRecord{a1b2c3 #7 A=com.example.app U=0 StackId=1 sz=1}",
            "      * Hist #0: ActivityRecord{deadbeef u0 com.example.app/.Main t7}",
            "    ResumedActivity: ActivityRecord{deadbeef u0 com.example.app/.Main t7}",
        ]
    )
    task = parse_foreground_task(dump)
    assert task is not None
    assert task.task_id == 7 and task.affinity == "com.example.app"
    assert task.owns("com.example.app")


@patch("artemis.utils.app_launch_utils.get_adb_device")
def test_get_foreground_task_tolerates_missing_device_and_errors(mock_get_device, mock_context):
    mock_get_device.return_value = None
    assert get_foreground_task(mock_context) is None

    device = Mock()
    device.shell.side_effect = RuntimeError("adb gone")
    mock_get_device.return_value = device
    assert get_foreground_task(mock_context) is None


# ---------------------------------------------------------------------------
# launch_app_with_retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("artemis.utils.app_launch_utils.get_foreground_task")
@patch("artemis.utils.app_launch_utils.UnifiedMobileController")
@patch("artemis.utils.app_launch_utils.get_current_foreground_package_async")
async def test_launch_app_success_immediate(
    mock_get_foreground, mock_controller_cls, mock_get_task, mock_context
):
    """String-equal foreground package is the fast path; the task stack is not read."""
    mock_controller = _controller(mock_controller_cls)
    mock_get_foreground.return_value = YOUTUBE

    success, error_msg = await launch_app_with_retries(
        mock_context, YOUTUBE, max_retries=1, max_poll_seconds=2
    )

    assert success is True
    assert error_msg is None
    mock_controller.launch_app.assert_called_once_with(YOUTUBE)
    mock_get_task.assert_not_called()


@pytest.mark.asyncio
@patch("artemis.utils.app_launch_utils.get_foreground_task")
@patch("artemis.utils.app_launch_utils.UnifiedMobileController")
@patch("artemis.utils.app_launch_utils.get_current_foreground_package_async")
async def test_launch_app_success_when_top_task_belongs_to_app(
    mock_get_foreground, mock_controller_cls, mock_get_task, mock_context
):
    """The Settings search activity on top of the Settings task counts as loaded."""
    mock_controller = _controller(mock_controller_cls)
    mock_get_foreground.return_value = SETTINGS_SEARCH
    mock_get_task.return_value = parse_foreground_task(DUMP_SETTINGS_TASK_WITH_SEARCH_ON_TOP)

    success, error_msg = await launch_app_with_retries(
        mock_context, SETTINGS, max_retries=1, max_poll_seconds=2
    )

    assert success is True
    assert error_msg is None
    mock_controller.launch_app.assert_called_once_with(SETTINGS)
    mock_controller.terminate_app.assert_not_called()
    mock_get_task.assert_called_once_with(mock_context)


@pytest.mark.asyncio
@patch("artemis.utils.app_launch_utils.get_foreground_task")
@patch("artemis.utils.app_launch_utils.UnifiedMobileController")
@patch("artemis.utils.app_launch_utils.get_current_foreground_package_async")
async def test_launch_app_success_permission_overlay_in_app_task(
    mock_get_foreground, mock_controller_cls, mock_get_task, mock_context
):
    """A permission dialog launched into the app's task is accepted without a package list."""
    _controller(mock_controller_cls)
    mock_get_foreground.return_value = "com.google.android.permissioncontroller"
    mock_get_task.return_value = ForegroundTask(
        task_id=42,
        resumed_component="com.google.android.permissioncontroller/.GrantPermissionsActivity",
        resumed_package="com.google.android.permissioncontroller",
        base_package=YOUTUBE,
        packages=frozenset({YOUTUBE, "com.google.android.permissioncontroller"}),
    )

    success, error_msg = await launch_app_with_retries(
        mock_context, YOUTUBE, max_retries=1, max_poll_seconds=2
    )

    assert success is True
    assert error_msg is None


@pytest.mark.asyncio
@patch("artemis.utils.app_launch_utils.get_foreground_task")
@patch("artemis.utils.app_launch_utils.UnifiedMobileController")
@patch("artemis.utils.app_launch_utils.get_current_foreground_package_async")
async def test_launch_app_failure_when_top_task_is_another_app(
    mock_get_foreground, mock_controller_cls, mock_get_task, mock_context
):
    """Foreground task rooted in another package is still a failure, and the error names both."""
    mock_controller = _controller(mock_controller_cls)
    mock_get_foreground.return_value = LAUNCHER
    mock_get_task.return_value = parse_foreground_task(DUMP_HOME)

    success, error_msg = await launch_app_with_retries(
        mock_context, SETTINGS, max_retries=2, max_poll_seconds=1
    )

    assert success is False
    assert f"Failed to launch {SETTINGS}" in error_msg
    assert mock_controller.launch_app.call_count == 2
    assert mock_controller.terminate_app.call_count == 1


@pytest.mark.asyncio
@patch("artemis.utils.app_launch_utils.get_foreground_task")
@patch("artemis.utils.app_launch_utils.UnifiedMobileController")
@patch("artemis.utils.app_launch_utils.get_current_foreground_package_async")
async def test_launch_timeout_message_names_foreground_package_and_task(
    mock_get_foreground, mock_controller_cls, mock_get_task, mock_context
):
    _controller(mock_controller_cls)
    mock_get_foreground.return_value = SETTINGS_SEARCH
    mock_get_task.return_value = parse_foreground_task(DUMP_SEARCH_IN_OWN_TASK)

    with patch("artemis.utils.app_launch_utils.logger") as mock_logger:
        success, _ = await launch_app_with_retries(
            mock_context, SETTINGS, max_retries=1, max_poll_seconds=1
        )

    assert success is False
    timeout_messages = [
        call.args[0]
        for call in mock_logger.error.call_args_list
        if "Timeout waiting" in call.args[0]
    ]
    assert len(timeout_messages) == 1
    message = timeout_messages[0]
    assert f"Current foreground: {SETTINGS_SEARCH}" in message
    assert "task #120" in message
    assert f"base={SETTINGS_SEARCH}" in message
    assert f"resumed={SETTINGS_SEARCH}/.modules.search.SearchActivity" in message


@pytest.mark.asyncio
@patch("artemis.utils.app_launch_utils.get_foreground_task")
@patch("artemis.utils.app_launch_utils.UnifiedMobileController")
@patch("artemis.utils.app_launch_utils.get_current_foreground_package_async")
async def test_launch_app_null_focus_keeps_polling_without_reading_task(
    mock_get_foreground, mock_controller_cls, mock_get_task, mock_context
):
    """mCurrentFocus=null is a loading state: no task lookup, then success once focused."""
    _controller(mock_controller_cls)
    mock_get_foreground.side_effect = [None, None, YOUTUBE]

    success, error_msg = await launch_app_with_retries(
        mock_context, YOUTUBE, max_retries=1, max_poll_seconds=3
    )

    assert success is True
    assert error_msg is None
    mock_get_task.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_initial_app_launch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("artemis.utils.app_launch_utils.launch_app_with_retries")
@patch("artemis.utils.app_launch_utils.get_foreground_task")
@patch("artemis.utils.app_launch_utils.get_current_foreground_package_async")
async def test_initial_launch_skips_launch_when_app_task_is_already_foreground(
    mock_get_foreground, mock_get_task, mock_launch, mock_context
):
    mock_get_foreground.return_value = SETTINGS_SEARCH
    mock_get_task.return_value = parse_foreground_task(DUMP_SETTINGS_TASK_WITH_SEARCH_ON_TOP)

    result = await _handle_initial_app_launch(mock_context, SETTINGS)

    assert result.locked_app_initial_launch_success is True
    assert result.locked_app_initial_launch_error is None
    mock_launch.assert_not_called()


@pytest.mark.asyncio
@patch("artemis.utils.app_launch_utils.launch_app_with_retries", new_callable=AsyncMock)
@patch("artemis.utils.app_launch_utils.get_foreground_task")
@patch("artemis.utils.app_launch_utils.get_current_foreground_package_async")
async def test_initial_launch_launches_when_another_app_is_foreground(
    mock_get_foreground, mock_get_task, mock_launch, mock_context
):
    mock_get_foreground.return_value = LAUNCHER
    mock_get_task.return_value = parse_foreground_task(DUMP_HOME)
    mock_launch.return_value = (True, None)

    result = await _handle_initial_app_launch(mock_context, SETTINGS)

    assert result.locked_app_initial_launch_success is True
    mock_launch.assert_awaited_once_with(mock_context, SETTINGS)


def test_task_affinity_alone_identifies_the_owning_app():
    """Seen on a Pixel 10: the Settings task had lost its root activity and only
    the search companion remained in its history, but the task affinity
    (``com.android.settings.root``) still named the app."""
    from artemis.utils.app_launch_utils import ForegroundTask

    task = ForegroundTask(
        task_id=121,
        affinity="com.android.settings.root",
        resumed_component="com.google.android.settings.intelligence/.modules.search.SearchActivity",
        resumed_package="com.google.android.settings.intelligence",
        base_package="com.google.android.settings.intelligence",
        packages=frozenset({"com.google.android.settings.intelligence"}),
    )
    assert task.owns("com.android.settings")
    assert not task.owns("com.android.settingsx")
    assert not task.owns("com.google.android.apps.nexuslauncher")
