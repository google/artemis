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

import json
from unittest.mock import patch, MagicMock

import pytest

from apps.admin_console.services.model_service import ModelService


def test_get_active_model_info_pro_architecture():
    """Verify that pro profile returns Pro architecture while keeping real LLM model."""
    info = ModelService.get_active_model_info("pro")
    assert info["name"] == "Pro"
    assert info["architecture"] == "ARTEMIS Pro"
    assert info["provider"] == "google"
    assert "id" in info


def test_get_active_model_info_flash_architecture():
    """Verify that flash profile returns Flash architecture."""
    info = ModelService.get_active_model_info("flash")
    assert info["name"] == "Flash"
    assert info["architecture"] == "ARTEMIS Flash"
    assert info["provider"] == "google"


def test_resolve_session_profile_from_device_info():
    """Verify profile resolution from device_info JSON."""
    row = {"device_info": json.dumps({"profile": "pro"})}
    assert ModelService.resolve_session_profile(row) == "pro"

    row_flash = {"device_info": json.dumps({"profile": "flash"})}
    assert ModelService.resolve_session_profile(row_flash) == "flash"


def test_resolve_session_profile_from_agent_names():
    """Verify profile resolution from agent/trace names."""
    row = {"device_info": None}
    assert ModelService.resolve_session_profile(row, agent_names=["planner", "operator"]) == "pro"
    assert ModelService.resolve_session_profile(row, agent_names=["flashrunner"]) == "flash"
