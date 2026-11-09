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

"""The shipped default config must match the repository config.

A packaged build reads the template under ``artemis/resources/config`` when no
user config exists, so a template left behind silently reintroduces whatever
provider it still names. Edit ``config/artemis.jsonc`` and copy it here.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_CONFIG = REPO_ROOT / "config" / "artemis.jsonc"
BUNDLED_TEMPLATE = REPO_ROOT / "artemis" / "resources" / "config" / "artemis.jsonc"


def test_bundled_template_matches_the_repository_config():
    assert BUNDLED_TEMPLATE.read_text(encoding="utf-8") == REPOSITORY_CONFIG.read_text(
        encoding="utf-8"
    )
