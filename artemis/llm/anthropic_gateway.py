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

"""Anthropic gateway authentication at the LangChain/SDK boundary."""

from functools import cached_property
from typing import Any

from langchain_anthropic import ChatAnthropic
from pydantic import Field, SecretStr


class BearerChatAnthropic(ChatAnthropic):
    """Pass a native Bearer credential to both SDK clients without an API key."""

    auth_token: SecretStr = Field(exclude=True, repr=False)

    @property
    def lc_secrets(self) -> dict[str, str]:
        return {**super().lc_secrets, "auth_token": "ANTHROPIC_AUTH_TOKEN"}

    @cached_property
    def _client_params(self) -> dict[str, Any]:
        # ChatAnthropic has no public auth_token field and always passes an
        # api_key (even an empty one). Adding Authorization to default_headers
        # leaves X-Api-Key intact. Adapt the shared sync/async SDK parameters
        # so the explicit token prevents credential lookup from the environment.
        return {
            **super()._client_params,
            "api_key": None,
            "auth_token": self.auth_token.get_secret_value(),
        }
