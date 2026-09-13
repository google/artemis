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

from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder
from artemis.sdk.builders.task_request_builder import TaskRequestCommonBuilder


class BuildersWrapper:
    @property
    def AgentConfig(self) -> AgentConfigBuilder:
        return AgentConfigBuilder()

    @property
    def TaskDefaults(self) -> TaskRequestCommonBuilder:
        return TaskRequestCommonBuilder()


Builders = BuildersWrapper()
