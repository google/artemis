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

"""The Pro tuning summary the SDK persists with every session's device_info."""

from artemis.sdk.agent import run_tuning_summary
from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder


def test_flash_runs_persist_no_tuning():
    config = AgentConfigBuilder().build(validate_profiles=False)
    assert run_tuning_summary(config, "flash") is None


def test_pro_runs_persist_the_two_launcher_sliders():
    config = (
        AgentConfigBuilder()
        .with_verification_level("checkpoints")
        .with_explorer(pro_mode="ultra")
        .build(validate_profiles=False)
    )
    assert run_tuning_summary(config, "pro") == {
        "verification_level": "checkpoints",
        "explorer_mode": "ultra",
    }


def test_pro_summary_reflects_checker_switched_off():
    config = AgentConfigBuilder().with_verification_level("off").build(validate_profiles=False)
    assert run_tuning_summary(config, "pro")["verification_level"] == "off"
