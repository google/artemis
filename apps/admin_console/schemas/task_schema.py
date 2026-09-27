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

from pydantic import BaseModel, model_validator

from artemis.config.llm_override import normalize_llm_override


class RunRequest(BaseModel):
    goal: str | None = None
    goals: list[str] | None = None
    profile: str | None = "flash"
    expected_output: str | None = None
    enable_outputter: bool | None = None
    # Pro-profile tuning (ignored by the Flash profile): a coarse Checker preset
    # ('off' | 'final' | 'checkpoints' | 'strict') and the Explorer perception
    # version used by the Operator ('flash' | 'pro' | 'ultra').
    verification_level: str | None = None
    explorer_mode: str | None = None
    # Per-task LLM override: pins every model of this one task to `llm_model`
    # (optionally on `llm_provider`) without touching artemis.jsonc or any
    # global state. Omitted/blank means "use the configured nodes".
    llm_model: str | None = None
    llm_provider: str | None = None
    locked_app_package: str | None = None
    app_path: str | None = None
    device_serial: str | None = None
    ingress: str | None = "frontend"
    session_id: str | None = None
    conversation_id: str | None = None

    @model_validator(mode="after")
    def _normalize_llm_override(self) -> "RunRequest":
        """Normalise the override and reject an unusable one up front.

        Blank means "not requested", the provider is lower-cased against the
        supported set, and a provider without a model is refused so FastAPI
        answers 422 instead of the task dying mid-run on a broken endpoint.
        """
        self.llm_model, self.llm_provider = normalize_llm_override(
            self.llm_model, self.llm_provider
        )
        return self


class ReplayRequest(BaseModel):
    device_id: str
    user_submits: dict
    tool_name: str = "ask_explorer"
    replay_id: str | None = None


class StopRequest(BaseModel):
    session_id: str | None = None
    device_id: str | None = None
    all: bool = False
