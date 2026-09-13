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

"""Tests for in-process reload of managed .env credentials."""

import os

from artemis.config.settings import settings


def test_reload_applies_keys_present_in_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=reloaded-gemini\n")
    monkeypatch.setattr("artemis.config.paths.get_env_file", lambda: env_file)
    settings.set_api_key("google", "stale-google", persist_to_env=False)

    settings.reload_managed_env()

    assert settings.get_api_key("google").get_secret_value() == "reloaded-gemini"
    assert os.environ["GEMINI_API_KEY"] == "reloaded-gemini"


def test_reload_clears_empty_file_values(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=\n")
    monkeypatch.setattr("artemis.config.paths.get_env_file", lambda: env_file)
    settings.set_api_key("openai", "stale-openai", persist_to_env=False)

    settings.reload_managed_env()

    assert settings.get_api_key("openai") is None


def test_reload_leaves_keys_omitted_from_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("XAI_API_KEY=from-file\n")
    monkeypatch.setattr("artemis.config.paths.get_env_file", lambda: env_file)
    settings.set_api_key("anthropic", "keep-anthropic", persist_to_env=False)

    settings.reload_managed_env()

    assert settings.get_api_key("anthropic").get_secret_value() == "keep-anthropic"
    assert settings.get_api_key("xai").get_secret_value() == "from-file"


def test_reload_ignores_behavior_flags(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ARTEMIS_HIERARCHY_BACKEND=uiautomator\n"
        "OPENAI_BASE_URL=http://127.0.0.1:9\n"
        "GEMINI_API_KEY=flag-test\n"
    )
    monkeypatch.setattr("artemis.config.paths.get_env_file", lambda: env_file)
    original_backend = settings.ARTEMIS_HIERARCHY_BACKEND
    original_base_url = settings.OPENAI_BASE_URL

    settings.reload_managed_env()

    assert settings.ARTEMIS_HIERARCHY_BACKEND == original_backend
    assert settings.OPENAI_BASE_URL == original_base_url
    assert settings.get_api_key("google").get_secret_value() == "flag-test"
