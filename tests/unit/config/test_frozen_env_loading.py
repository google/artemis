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

"""A compiled bundle may carry its own dotenv file beside the executable."""

import importlib
from pathlib import Path

from artemis.config import paths

settings = importlib.import_module("artemis.config.settings")


def test_source_checkout_has_no_executable_env(monkeypatch):
    monkeypatch.setattr(paths, "is_frozen_bundle", lambda: False)

    assert paths.get_executable_env_file() is None


def test_frozen_bundle_reads_the_file_beside_the_binary(monkeypatch, tmp_path):
    executable = tmp_path / "artemis-cli"
    executable.write_text("", encoding="utf-8")
    monkeypatch.setattr(paths, "is_frozen_bundle", lambda: True)
    monkeypatch.setattr(paths.sys, "executable", str(executable))

    assert paths.get_executable_env_file() == tmp_path / ".env"


def test_executable_env_is_loaded_before_the_user_file(monkeypatch, tmp_path):
    executable_env = tmp_path / "bundle" / ".env"
    executable_env.parent.mkdir()
    executable_env.write_text("OPENAI_API_KEY=from-bundle\n", encoding="utf-8")
    user_env = tmp_path / "user" / ".env"
    user_env.parent.mkdir()
    user_env.write_text("OPENAI_API_KEY=from-user\n", encoding="utf-8")

    loaded_paths: list[Path] = []
    monkeypatch.setattr(settings, "get_executable_env_file", lambda: executable_env)
    monkeypatch.setattr(settings, "get_env_file", lambda: user_env)
    monkeypatch.setattr(settings, "GLOBAL_APP_DIR", tmp_path / "missing")
    monkeypatch.setattr(
        settings,
        "load_dotenv",
        lambda dotenv_path=None, verbose=False: loaded_paths.append(dotenv_path),
    )

    ordered = settings.load_env_files()

    assert ordered == [executable_env, user_env]
    assert loaded_paths == [executable_env, user_env]


def test_a_missing_executable_env_is_skipped(monkeypatch, tmp_path):
    user_env = tmp_path / "user" / ".env"
    user_env.parent.mkdir()
    user_env.write_text("", encoding="utf-8")

    monkeypatch.setattr(settings, "get_executable_env_file", lambda: tmp_path / "absent" / ".env")
    monkeypatch.setattr(settings, "get_env_file", lambda: user_env)
    monkeypatch.setattr(settings, "GLOBAL_APP_DIR", tmp_path / "missing")
    monkeypatch.setattr(settings, "load_dotenv", lambda dotenv_path=None, verbose=False: None)

    assert settings.load_env_files() == [user_env]


def test_the_user_file_is_not_loaded_twice(monkeypatch, tmp_path):
    user_env = tmp_path / "artemis" / ".env"
    user_env.parent.mkdir()
    user_env.write_text("", encoding="utf-8")

    monkeypatch.setattr(settings, "get_executable_env_file", lambda: None)
    monkeypatch.setattr(settings, "get_env_file", lambda: user_env)
    monkeypatch.setattr(settings, "GLOBAL_APP_DIR", user_env.parent)
    monkeypatch.setattr(settings, "load_dotenv", lambda dotenv_path=None, verbose=False: None)

    assert settings.load_env_files() == [user_env]
