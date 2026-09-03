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

"""Engine, turn budget, tools and caching settings for each Explorer tier.

User configuration selects the tier. The calling agent sees the same
``ask_explorer`` contract at every tier.
"""

from dataclasses import dataclass
from typing import Literal

from artemis.config.constants import ExplorerVersion

ExplorerEngine = Literal["oneshot", "loop"]

#: Tool that terminates a reasoning loop; always exposed by loop engines.
SUBMIT_TOOL = "submit_answer"

#: Every perception / transformation tool the loop engines can expose.
PERCEPTION_TOOLS: frozenset[str] = frozenset(
    {
        "ask_perception_tool",
        "detect_objects",
        "get_ocr_list",
        "inspect_region",
        "ask_image_processor",
    }
)


@dataclass(frozen=True)
class ExplorerTier:
    """Behavioral profile of one Explorer tier."""

    name: ExplorerVersion
    engine: ExplorerEngine
    #: Reasoning turns for the loop engine (1 for one-shot detection).
    max_turns: int
    #: Perception tools exposed to the loop engine (``submit_answer`` is implicit).
    tools: frozenset[str]
    #: Default for Gemini explicit context caching when the user did not decide.
    caching: bool
    #: One-line summary used in logs and configuration help.
    summary: str

    @property
    def is_oneshot(self) -> bool:
        return self.engine == "oneshot"

    @property
    def hidden_tools(self) -> frozenset[str]:
        """Perception tools this tier does not expose."""
        return PERCEPTION_TOOLS - self.tools


EXPLORER_TIERS: dict[ExplorerVersion, ExplorerTier] = {
    "flash": ExplorerTier(
        name="flash",
        engine="oneshot",
        max_turns=1,
        tools=frozenset(),
        caching=False,
        summary="One-shot visual detection on the current screenshot; fastest, no reasoning loop.",
    ),
    "pro": ExplorerTier(
        name="pro",
        engine="loop",
        max_turns=3,
        tools=frozenset({"ask_perception_tool"}),
        caching=False,
        summary=(
            "Short reasoning loop combining UI-tree search, coordinate audit and visual"
            " detection; balanced default."
        ),
    ),
    "ultra": ExplorerTier(
        name="ultra",
        engine="loop",
        max_turns=8,
        tools=PERCEPTION_TOOLS,
        caching=True,
        summary=(
            "Deep reasoning loop with zooming, OCR and pixel-level image processing for"
            " layout-critical searches; slowest."
        ),
    ),
}

DEFAULT_TIER: ExplorerVersion = "flash"


def get_tier(version: str | None) -> ExplorerTier:
    """Returns the tier for ``version``; unknown or empty names fall back to the default."""
    key = str(version or "").strip().lower()
    for tier in EXPLORER_TIERS.values():
        if tier.name == key:
            return tier
    return EXPLORER_TIERS[DEFAULT_TIER]
