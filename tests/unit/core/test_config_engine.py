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

"""Unit tests for CascadingConfigEngine and variable interpolation."""

from unittest.mock import patch

from artemis.config.core import CascadingConfigEngine, interpolate_config_value


def test_interpolate_env_variables():
    """Verify ${env:KEY} expressions are properly interpolated."""
    with patch.dict("os.environ", {"TEST_KEY": "secret_123"}):
        result = interpolate_config_value("Bearer ${env:TEST_KEY}")
        assert result == "Bearer secret_123"

        default_result = interpolate_config_value("${env:MISSING_KEY:-fallback_val}")
        assert default_result == "fallback_val"


def test_interpolate_path_variables():
    """Verify ${path:KEY} expressions resolve system standard paths."""
    result = interpolate_config_value("${path:temp_dir}/screenshots")
    assert "/screenshots" in result or "\\screenshots" in result


def test_cascading_config_merge():
    """Verify CLI overrides take precedence in CascadingConfigEngine."""
    engine = CascadingConfigEngine()
    merged = engine.load_raw_config(cli_overrides={"default": {"model": "gemini-test-custom"}})
    assert merged.get("default", {}).get("model") == "gemini-test-custom"
