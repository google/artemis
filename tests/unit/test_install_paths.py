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

"""Regression tests for source-checkout versus installed-wheel state paths."""

from artemis.config import paths
from artemis.config import runtime


def _clear_path_overrides(monkeypatch):
    for name in (
        "ARTEMIS_APP_DIR",
        "ANTIGRAVITY_APP_DIR",
        "ARTEMIS_TRACES_DIR",
        "ARTEMIS_USE_USER_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def test_installed_package_keeps_mutable_state_out_of_site_packages(tmp_path, monkeypatch):
    _clear_path_overrides(monkeypatch)
    installed_root = tmp_path / "site-packages"
    (installed_root / "artemis").mkdir(parents=True)
    user_app_dir = tmp_path / "user-data" / "artemis"

    monkeypatch.setattr(paths, "ROOT_DIR", installed_root)
    monkeypatch.setattr(paths.platform.paths, "resolve_app_dir", lambda: user_app_dir)

    assert not paths.is_source_checkout()
    assert paths.get_env_file() == user_app_dir / ".env"
    assert paths.get_default_traces_path() == user_app_dir / "traces"
    assert paths.get_ipc_port_file().parent == user_app_dir
    assert paths.get_ls_address_file().parent == user_app_dir
    assert paths.get_server_info_file().parent == user_app_dir
    assert paths.get_pause_file().parent == user_app_dir


def test_source_checkout_preserves_workspace_defaults(tmp_path, monkeypatch):
    _clear_path_overrides(monkeypatch)
    source_root = tmp_path / "checkout"
    (source_root / "artemis").mkdir(parents=True)
    (source_root / "pyproject.toml").write_text("[project]\nname='artemis'\n", encoding="utf-8")

    monkeypatch.setattr(paths, "ROOT_DIR", source_root)
    # A developer machine may carry a real user-level port file; the source
    # checkout contract must not depend on that ambient state.
    monkeypatch.setattr(
        paths.platform.paths, "resolve_app_dir", lambda: tmp_path / "user-data" / "artemis"
    )

    assert paths.is_source_checkout()
    assert paths.get_env_file() == source_root / ".env"
    assert paths.get_default_traces_path() == source_root / "traces"
    assert paths.get_ipc_port_file().parent == source_root
    assert paths.get_pause_file().parent == source_root


def test_installed_ipc_never_writes_site_packages_legacy_file(tmp_path, monkeypatch):
    _clear_path_overrides(monkeypatch)
    installed_root = tmp_path / "site-packages"
    (installed_root / "artemis").mkdir(parents=True)
    user_app_dir = tmp_path / "user-data" / "artemis"
    temp_dir = tmp_path / "temp"

    monkeypatch.setattr(paths, "ROOT_DIR", installed_root)
    monkeypatch.setattr(runtime, "ROOT_DIR", installed_root)
    monkeypatch.setattr(paths.platform.paths, "resolve_app_dir", lambda: user_app_dir)
    monkeypatch.setattr(runtime, "get_temp_dir", lambda: temp_dir)

    runtime.write_ipc_port(43123)
    assert (user_app_dir / ".artemis_ipc_port").read_text(encoding="utf-8") == "43123"
    assert not (installed_root / ".artemis_ipc_port").exists()

    runtime.clear_ipc_port()
