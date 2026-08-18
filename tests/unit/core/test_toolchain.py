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

"""Unit tests for ToolchainResolver and binary discovery."""

from pathlib import Path
from unittest.mock import patch

from artemis.toolchain import ToolchainResolver, find_adb, find_ffmpeg, find_scrcpy


def test_toolchain_env_override():
    """Verify explicit environment variables override tool path resolution."""
    resolver = ToolchainResolver()
    fake_adb = Path("/tmp/fake_adb_binary")
    fake_adb.touch(exist_ok=True)

    with patch.dict("os.environ", {"ARTEMIS_ADB_PATH": str(fake_adb)}):
        resolved = resolver.resolve("adb", force_refresh=True)
        assert resolved == str(fake_adb.resolve())


def test_toolchain_helper_functions():
    """Verify toolchain discovery helper functions return strings."""
    adb = find_adb()
    ffmpeg = find_ffmpeg()
    scrcpy = find_scrcpy()

    assert isinstance(adb, str) and len(adb) > 0
    assert isinstance(ffmpeg, str) and len(ffmpeg) > 0
    assert isinstance(scrcpy, str) and len(scrcpy) > 0


def test_toolchain_is_installed_lookup():
    """Verify is_installed check on known and unknown tools."""
    resolver = ToolchainResolver()
    assert resolver.is_installed("non_existent_tool_xyz123") is False
