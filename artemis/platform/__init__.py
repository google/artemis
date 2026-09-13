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

"""Platform Abstraction Layer (PAL) single entry point and global singleton."""

import sys

from artemis.platform.base import (
    IPlatform,
    IPlatformPaths,
    IPlatformProcess,
    OSType,
)
from artemis.platform.darwin import DarwinPlatform
from artemis.platform.linux import LinuxPlatform
from artemis.platform.windows import WindowsPlatform


def _create_platform() -> IPlatform:
    """Factory creating the appropriate platform instance for current runtime environment."""
    if sys.platform == "win32":
        p = WindowsPlatform()
    elif sys.platform == "darwin":
        p = DarwinPlatform()
    else:
        p = LinuxPlatform()

    # Automatically initialize UTF-8 environment
    p.process.setup_utf8_io()
    return p


# Global singleton PAL instance
platform: IPlatform = _create_platform()


def get_platform() -> IPlatform:
    """Get the active platform abstraction instance."""
    global platform
    return platform


__all__ = [
    "platform",
    "get_platform",
    "OSType",
    "IPlatform",
    "IPlatformPaths",
    "IPlatformProcess",
]
