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

"""Admin Console configuration facade.

All configuration and path management is centralized in `artemis.config`.
This module re-exports common constants for full backward compatibility.
"""

from artemis.config import (
    DB_PATH,
    IMAGES_DIR,
    PAUSE_FILE,
    REPLAY_BASE_DIR,
    TEST_DATA_DIR,
    TEST_OUTPUTS_DIR,
    TRACES_PATH,
    WORKSPACE_ROOT,
    init_ls_address,
)

__all__ = [
    "DB_PATH",
    "IMAGES_DIR",
    "PAUSE_FILE",
    "REPLAY_BASE_DIR",
    "TEST_DATA_DIR",
    "TEST_OUTPUTS_DIR",
    "TRACES_PATH",
    "WORKSPACE_ROOT",
    "init_ls_address",
]
