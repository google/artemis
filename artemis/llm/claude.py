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

"""Claude model capability checks for the Anthropic request builder.

Newer Claude models reject request shapes older ones accept:

* Opus 4.7+, Sonnet 5+, Haiku 5+ and every Fable / Mythos model are
  *adaptive-only*: ``thinking.budget_tokens`` and non-default sampling
  parameters (``temperature``) are a 400, and thinking depth is set with
  ``output_config.effort``. The Fable 5.1 / Opus 5.5 / Sonnet 5.5
  generation also rejects forced ``tool_choice``.
* Fable 5.1, Opus 5.5, Sonnet 5.5 and Haiku 5.5 bind each thinking block to
  the exact history that produced it ("preserved thinking"). Replaying a
  block after an earlier message changed is a 400 unless the request opts
  into ``prefix_mismatch_behavior: "drop_block"``.

Model ids with no parsable family/version (``claude-3-7-sonnet-...``,
custom deployments) are treated as legacy and keep the old request shape.
"""

import re
from typing import Any

#: Beta header that unlocks ``thinking.block_binding``.
THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"

#: Accepted values of ``output_config.effort``.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

_MODEL_RE = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?!\d)")

# Lowest version in each family that is adaptive-only.
_ADAPTIVE_ONLY_MIN = {
    "opus": (4, 7),
    "sonnet": (5, 0),
    "haiku": (5, 0),
    "fable": (0, 0),
    "mythos": (0, 0),
}

# Lowest version in each family that binds thinking blocks to the history.
_BLOCK_BINDING_MIN = {
    "opus": (5, 5),
    "sonnet": (5, 5),
    "haiku": (5, 5),
    "fable": (5, 1),
    "mythos": (5, 1),
}


def claude_version(model_name: Any) -> tuple[str, tuple[int, int]] | None:
    """Parses ``claude-sonnet-5-5`` into ``("sonnet", (5, 5))``.

    Provider prefixes (``anthropic.``, ``anthropic/``) and date or context
    suffixes are ignored. Returns ``None`` for ids in the legacy
    ``claude-<major>-<minor>-<family>`` order or anything unrecognised.
    """
    if not isinstance(model_name, str):
        return None
    match = _MODEL_RE.search(model_name.lower())
    if not match:
        return None
    # The minor group stops at two digits, so a date snapshot
    # (``claude-opus-4-20250514``) leaves the minor version at 0.
    family, major, minor = match.groups()
    return family, (int(major), int(minor) if minor else 0)


def _at_least(model_name: Any, minimums: dict[str, tuple[int, int]]) -> bool:
    parsed = claude_version(model_name)
    if parsed is None:
        return False
    family, version = parsed
    return version >= minimums[family]


def is_adaptive_only(model_name: Any) -> bool:
    """Whether the model rejects ``budget_tokens`` and non-default sampling."""
    return _at_least(model_name, _ADAPTIVE_ONLY_MIN)


def binds_thinking_blocks(model_name: Any) -> bool:
    """Whether replayed thinking blocks must match their original history."""
    return _at_least(model_name, _BLOCK_BINDING_MIN)


def effort_for(reasoning_effort: str | None, thinking_budget: int | None) -> str | None:
    """Maps the endpoint's effort / budget settings onto ``output_config.effort``.

    An explicit ``reasoning_effort`` wins; a legacy token budget is bucketed
    with the same thresholds the budget path uses (2048 / 8192 / 32768).
    """
    if reasoning_effort:
        effort = reasoning_effort.lower()
        return effort if effort in EFFORT_LEVELS else None
    if thinking_budget:
        if thinking_budget <= 2048:
            return "low"
        if thinking_budget <= 8192:
            return "medium"
        return "high"
    return None
