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

"""Contract tests for release metadata and bundled runtime resources."""

from importlib.metadata import version

import artemis
from artemis import resources


def test_public_version_comes_from_distribution_metadata():
    assert artemis.__version__ == version("artemis")


def test_source_tree_contains_complete_release_resources():
    config_path = resources.get_bundled_config_path("artemis.jsonc")
    showcase_path = resources.get_bundled_showcase_dist()

    assert config_path is not None and config_path.is_file()
    assert showcase_path is not None and (showcase_path / "index.html").is_file()


def test_bundled_resource_accessors_require_complete_assets(tmp_path, monkeypatch):
    resource_root = tmp_path / "resources"
    config_dir = resource_root / "config"
    showcase_dir = resource_root / "showcase_ui"
    config_dir.mkdir(parents=True)
    showcase_dir.mkdir()
    (config_dir / "artemis.jsonc").write_text("{}", encoding="utf-8")
    (showcase_dir / "index.html").write_text("<html></html>", encoding="utf-8")

    monkeypatch.setattr(resources, "files", lambda _package: resource_root)

    assert resources.get_bundled_config_path("artemis.jsonc") == (config_dir / "artemis.jsonc")
    assert resources.get_bundled_config_path("missing.jsonc") is None
    assert resources.get_bundled_showcase_dist() == showcase_dir


def test_incomplete_showcase_resource_is_rejected(tmp_path, monkeypatch):
    resource_root = tmp_path / "resources"
    (resource_root / "showcase_ui").mkdir(parents=True)
    monkeypatch.setattr(resources, "files", lambda _package: resource_root)

    assert resources.get_bundled_showcase_dist() is None
