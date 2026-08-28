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

"""Unit tests for Platform Abstraction Layer (PAL)."""

from pathlib import Path
from unittest.mock import patch

from artemis.platform import (
    DarwinPlatform,
    LinuxPlatform,
    OSType,
    WindowsPlatform,
    get_platform,
)


def test_platform_singleton():
    """Verify global platform singleton is initialized and conforms to protocol."""
    p = get_platform()
    assert p is not None
    assert p.os_type in (OSType.LINUX, OSType.MACOS, OSType.WINDOWS)
    assert p.paths.config_dir is not None
    assert p.paths.data_dir is not None
    assert p.paths.temp_dir() is not None
    assert p.process.path_separator in (":", ";")


def test_linux_platform_paths():
    """Verify Linux platform paths conform to XDG Base Directory specification."""
    with patch.dict(
        "os.environ",
        {"XDG_CONFIG_HOME": "/tmp/test_xdg_config", "XDG_DATA_HOME": "/tmp/test_xdg_data"},
        clear=False,
    ):
        linux_p = LinuxPlatform()
        assert linux_p.os_type == OSType.LINUX
        assert linux_p.paths.config_dir == Path("/tmp/test_xdg_config/artemis")
        assert linux_p.paths.data_dir == Path("/tmp/test_xdg_data/artemis")
        assert linux_p.process.path_separator == ":"


def test_darwin_platform_paths():
    """Verify macOS platform paths adhere to Apple Library standards."""
    darwin_p = DarwinPlatform()
    assert darwin_p.os_type == OSType.MACOS
    # Compare path components so the assertion holds regardless of the host
    # OS path separator used by str().
    assert darwin_p.paths.config_dir.parts[-3:] == ("Library", "Application Support", "artemis")
    assert darwin_p.process.path_separator == ":"


def test_windows_platform_paths():
    """Verify Windows platform paths adhere to AppData standards."""
    with patch.dict(
        "os.environ",
        {
            "APPDATA": "C:\\Users\\Test\\AppData\\Roaming",
            "LOCALAPPDATA": "C:\\Users\\Test\\AppData\\Local",
        },
        clear=False,
    ):
        with patch.object(Path, "mkdir", return_value=None):
            win_p = WindowsPlatform()
            assert win_p.os_type == OSType.WINDOWS
            assert str(win_p.paths.config_dir).startswith("C:\\Users\\Test\\AppData\\Roaming")
            assert str(win_p.paths.data_dir).startswith("C:\\Users\\Test\\AppData\\Local")
            assert win_p.process.path_separator == ";"
            assert win_p.get_install_command("adb") == "winget install Google.PlatformTools"
