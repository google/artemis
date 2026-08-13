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

"""LLM Provider Client Factory."""

from langchain_core.language_models.chat_models import BaseChatModel
from artemis.llm.router import ModelEndpoint, default_router


def get_chat_model(role: str = "operator", endpoint: ModelEndpoint | None = None) -> BaseChatModel:
    """Instantiates a configured chat model for the requested role or endpoint."""
    if endpoint:
        return default_router.instantiate_model(endpoint)
    return default_router.create_chat_model(role)
