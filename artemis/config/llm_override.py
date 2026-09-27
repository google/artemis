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

"""One normalisation/validation pass for the per-task LLM override.

Every entry point that can carry an override (the Console API schema, the SDK
builder, the MCP tool, the MCP background worker and the queue service)
funnels the pair through
:func:`normalize_llm_override` so one typo is reported the same way everywhere,
before any trace or task exists.

Semantics:

* ``model`` is a raw provider model identifier and stays case-sensitive, so only
  surrounding whitespace is stripped.
* ``provider`` names an endpoint, so it is stripped, lower-cased and checked
  against :data:`SUPPORTED_LLM_PROVIDERS`.
* Blank/whitespace-only input means "not requested" on both sides.
* A provider without a model is rejected: a provider only says *where* to send
  the model, so accepting it alone would pin nothing and fail mid-task on a
  broken endpoint.
"""

from typing import get_args

from artemis.config.constants import LLMProvider

#: Providers accepted by the per-task override. Single source of truth: the same
#: literal the LLM config validates against.
SUPPORTED_LLM_PROVIDERS: tuple[str, ...] = get_args(LLMProvider)


def normalize_llm_override(
    llm_model: object = None,
    llm_provider: object = None,
    *,
    error_prefix: str = "",
) -> tuple[str | None, str | None]:
    """Normalise and validate a per-task ``(model, provider)`` override.

    Args:
        llm_model: Model identifier, or anything blank/``None`` for "unset".
        llm_provider: Provider name, normalised to the API's lower-case
            spelling and checked against the supported set.
        error_prefix: Optional prefix for the ``ValueError`` messages, used by
            the SDK builder to name the method that failed.

    Returns:
        The ``(model, provider)`` pair with blanks collapsed to ``None`` and
        the provider lower-cased.

    Raises:
        ValueError: If the provider is not a supported one, or if a provider is
            given without a model.
    """
    model = str(llm_model).strip() or None if llm_model is not None else None
    raw_provider = str(llm_provider).strip() if llm_provider is not None else ""
    provider = raw_provider.lower() or None
    if provider is not None and provider not in SUPPORTED_LLM_PROVIDERS:
        raise ValueError(
            f"{error_prefix}unknown llm_provider {llm_provider!r}. "
            "Supported providers: " + ", ".join(SUPPORTED_LLM_PROVIDERS)
        )
    if provider and not model:
        raise ValueError(
            f"{error_prefix}llm_provider requires llm_model (got llm_provider={llm_provider!r}): "
            "pass llm_model too, or drop llm_provider."
        )
    return model, provider
