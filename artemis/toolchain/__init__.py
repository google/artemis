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

"""Managed Toolchain and Binary Discovery Subsystem."""

from artemis.toolchain.descriptors import TOOLS, ToolDescriptor
from artemis.toolchain.resolver import ToolchainResolver, toolchain

find_adb = toolchain.find_adb
find_ffmpeg = toolchain.find_ffmpeg
find_scrcpy = toolchain.find_scrcpy
is_installed = toolchain.is_installed

__all__ = [
    "toolchain",
    "ToolchainResolver",
    "ToolDescriptor",
    "TOOLS",
    "find_adb",
    "find_ffmpeg",
    "find_scrcpy",
    "is_installed",
]
