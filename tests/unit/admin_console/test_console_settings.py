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

"""Console settings persistence.

``SettingsService`` backs ``GET/PUT /api/system/settings``: each key is stored
JSON-encoded in the ``app_settings`` table so a console preference survives a
daemon restart. These tests bind the service to a throwaway database.
"""

import importlib

import pytest

connection = importlib.import_module("apps.admin_console.database.connection")
settings_module = importlib.import_module("apps.admin_console.services.settings_service")


@pytest.fixture
def service(tmp_path, monkeypatch):
    """A SettingsService writing to an isolated database."""
    monkeypatch.setattr(connection, "DB_PATH", tmp_path / "settings.db")
    return settings_module.SettingsService()


def test_defaults_are_returned_before_any_write(service):
    assert service.get_all() == {"screen_off": False}
    assert service.get("screen_off") is False


def test_update_persists_across_instances(service):
    service.update({"screen_off": True})

    assert service.get("screen_off") is True
    # A second instance stands in for a restarted process reading the same row.
    assert settings_module.SettingsService().get_all()["screen_off"] is True


def test_unknown_keys_round_trip(service):
    service.update({"ui.wifi_host": "10.0.0.5", "nested": {"a": 1}})

    assert service.get("ui.wifi_host") == "10.0.0.5"
    assert service.get("nested") == {"a": 1}
    assert service.get_all()["ui.wifi_host"] == "10.0.0.5"


def test_get_falls_back_for_unset_key(service):
    assert service.get("never_set", "fallback") == "fallback"


def test_update_returns_the_merged_snapshot(service):
    merged = service.update({"screen_off": True})

    assert merged == {"screen_off": True}


def test_update_rejects_a_non_object_payload(service):
    with pytest.raises(ValueError):
        service.update(["not", "a", "mapping"])
