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

import os
from pathlib import Path
import sys


# Automatically write ANTIGRAVITY_LS_ADDRESS to shared synchronization file on startup
# to allow decoupled background processes to communicate with Jetski
def init_ls_address():
    ls_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
    if ls_addr:
        try:
            from artemis.config import write_ls_address

            write_ls_address(ls_addr)
        except Exception as e:
            sys.stderr.write(f"Failed to write .jetski_ls_address on startup: {e}\n")


# Compute real WORKSPACE_ROOT (find repo root containing pyproject.toml or artemis)
_current_p = Path(__file__).resolve().parent
while _current_p != _current_p.parent:
    if (_current_p / "pyproject.toml").exists() or (_current_p / "artemis").is_dir():
        WORKSPACE_ROOT = _current_p
        break
    _current_p = _current_p.parent
else:
    WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent

_apps_dir = WORKSPACE_ROOT / "apps"
_admin_console_dir = _apps_dir / "admin_console"
_cloud_service_dir = _apps_dir / "cloud_service"

for _p in (str(WORKSPACE_ROOT), str(_apps_dir), str(_admin_console_dir), str(_cloud_service_dir)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Try to import settings from artemis
try:
    from artemis.config import settings

    TRACES_PATH = Path(settings.TRACES_PATH)
    DB_PATH = Path(settings.DATA_ENGINE_DB_PATH)
except ImportError:
    # Fallback to default path if run outside project environment or imports fail
    TRACES_PATH = WORKSPACE_ROOT / "traces"
    DB_PATH = TRACES_PATH / "data_engine.db"

# Step Replay Directories (Stored within traces/replay to avoid workspace clutter)
REPLAY_BASE_DIR = TRACES_PATH / "replay"
TEST_DATA_DIR = REPLAY_BASE_DIR / "data"
TEST_OUTPUTS_DIR = REPLAY_BASE_DIR / "outputs"

IMAGES_DIR = TRACES_PATH / "images"
PAUSE_FILE = WORKSPACE_ROOT / ".artemis_paused"
