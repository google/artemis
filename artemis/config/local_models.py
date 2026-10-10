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

"""Catalog of local models ARTEMIS can pull and serve itself.

Each alias maps to a Hugging Face repository servable by ``mlx_vlm.server``
(``pip install artemis[local]`` on Apple Silicon). Aliases are what the
``"model"`` field in ``artemis.jsonc`` references when ``provider`` is
``"custom"`` and ``api_base`` points at the managed server.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LocalModelSpec:
    """A local model ARTEMIS can pull and serve."""

    repo: str
    description: str = ""
    # Recommended ``max_soft_tokens`` for the model's image processor —
    # the vision-token budget per image. Gemma 4 ships with 280, which
    # downsamples a phone screenshot to ~528x1152 effective; 1120 keeps
    # near-original detail (~1056x2352) and measurably improves detection
    # and not-found discipline. ``None`` leaves the shipped default.
    vision_soft_tokens: int | None = None


LOCAL_MODELS: dict[str, LocalModelSpec] = {
    "gemma4-e4b": LocalModelSpec(
        repo="mlx-community/gemma-4-e4b-it-4bit",
        description="Gemma 4 E4B 4-bit multimodal (tested; ~5 GB pull, ~6 GB resident)",
        vision_soft_tokens=1120,
    ),
}

DEFAULT_LOCAL_MODEL = "gemma4-e4b"


def resolve_model_ref(alias_or_repo: str) -> tuple[str, str] | None:
    """Map a catalog alias or ``org/repo`` id to ``(alias, repo)``.

    Returns ``None`` for bare names that match neither the catalog nor the
    ``org/repo`` shape — those cannot be served by ARTEMIS itself.
    """
    spec = LOCAL_MODELS.get(alias_or_repo)
    if spec is not None:
        return alias_or_repo, spec.repo
    if "/" in alias_or_repo:
        return alias_or_repo, alias_or_repo
    return None
