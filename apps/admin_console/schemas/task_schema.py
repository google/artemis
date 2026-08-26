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

from pydantic import BaseModel


class RunRequest(BaseModel):
    goal: str | None = None
    goals: list[str] | None = None
    profile: str | None = "flash"
    expected_output: str | None = None
    enable_outputter: bool | None = None
    locked_app_package: str | None = None
    app_path: str | None = None
    device_serial: str | None = None



class ReplayRequest(BaseModel):
    device_id: str
    user_submits: dict
    tool_name: str = "ask_explorer"
    replay_id: str | None = None
