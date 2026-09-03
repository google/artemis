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

"""``ask_explorer``: locate UI elements through the Explorer sub-agent.

The module is layered so that every caller (Pro Operator via LangChain, the
Validator / Flash runner tool executor, the MCP action executor, replay) shares
one pipeline and differs only in presentation:

1. :func:`locate` runs the user-configured Explorer tier and parses its answer
   into an :class:`ExplorerOutcome`.
2. :func:`register_candidates` appends the located elements to the calling
   agent's indexed element list so they can be acted on by index.
3. :func:`render_text` / :func:`render_operator_blocks` present the result.

The Explorer tier (flash / pro / ultra) is a user setting resolved from
configuration; it is never part of the tool contract shown to agents.
"""

import base64
from dataclasses import dataclass, field
import glob
import json
from pathlib import Path
import re
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from artemis.agents.explorer.constants import (
    ASK_EXPLORER_CONTEXT_FEEDBACK_DESCRIPTION,
    ASK_EXPLORER_DESCRIPTION,
    ASK_EXPLORER_QUERY_DESCRIPTION,
    ASK_EXPLORER_TOOL_NAME,
)
from artemis.agents.explorer.explorer import Explorer
from artemis.agents.explorer.geometry import (
    is_valid_norm_point,
    norm_to_pixel,
    resolve_screen_size,
)
from artemis.agents.explorer.tiers import get_tier
from artemis.config import ExplorerVersion, resolve_explorer_version, settings
from artemis.context import ArtemisContext
from artemis.drivers.base import BaseDeviceDriver
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolCategory, ToolRegistry
from artemis.tools.tool_wrapper import ToolWrapper
from artemis.tools.types import CyFunctionDetector
from artemis.utils.element_hit_test import find_element_at_point
from artemis.utils.logger import get_logger
from artemis.utils.visualization import draw_dots

logger = get_logger(__name__)

__all__ = [
    "AskExplorerArgs",
    "AskExplorerTool",
    "ExplorerCandidate",
    "ExplorerOutcome",
    "RegisteredCandidate",
    "ask_explorer",
    "ask_explorer_text",
    "ask_explorer_wrapper",
    "get_ask_explorer_tool",
    "locate",
    "register_candidates",
    "render_operator_blocks",
    "render_text",
    "resolve_explorer_version",
]

#: Color of the dots drawn for newly located candidates; distinct from the
#: operator's own element numbering so the new indices stand out.
CANDIDATE_DOT_COLOR = "magenta"


# --------------------------------------------------------------------------- #
# Tool contract (tier-agnostic)
# --------------------------------------------------------------------------- #


class AskExplorerArgs(BaseModel):
    """Arguments of ``ask_explorer`` as seen by every calling agent."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    query: str = Field(..., description=ASK_EXPLORER_QUERY_DESCRIPTION)
    context_feedback: str = Field("", description=ASK_EXPLORER_CONTEXT_FEEDBACK_DESCRIPTION)


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #


NormBounds = tuple[int, int, int, int]


def _parse_norm_bounds(value: Any) -> NormBounds | None:
    """Accepts ``[l, t, r, b]`` in the 0-1000 scale with a positive area."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        left, top, right, bottom = (int(v) for v in value)
    except (TypeError, ValueError):
        return None
    if not all(0 <= v <= 1000 for v in (left, top, right, bottom)):
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


@dataclass(frozen=True)
class ExplorerCandidate:
    """One element the Explorer located, in normalized 0-1000 coordinates.

    ``bounds`` is present when the Explorer could tie the candidate to a
    UI-tree or OCR element; it lets the pre-execution safety net verify the
    target like any other indexed element.
    """

    label: str
    coords: tuple[int, int]
    description: str
    bounds: NormBounds | None = None
    #: "xml", "ocr", or "" when the candidate is a bare visual detection.
    source: str = ""


@dataclass
class ExplorerOutcome:
    """Parsed answer of one Explorer run."""

    candidates: list[ExplorerCandidate] = field(default_factory=list)
    #: Fallback explanation or extra observations from the Explorer.
    message: str = ""
    #: True when the run itself failed (as opposed to a clean "not found").
    error: bool = False
    #: Raw Explorer answer, kept for traces and replay.
    raw: str = ""

    @property
    def found(self) -> bool:
        return bool(self.candidates)

    @classmethod
    def failure(cls, message: str, raw: str = "") -> "ExplorerOutcome":
        return cls(message=message, error=True, raw=raw)

    @classmethod
    def from_raw(cls, raw: Any, default_label: str = "") -> "ExplorerOutcome":
        """Parses the Explorer's JSON answer, dropping malformed candidates."""
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return cls.failure(f"Explorer returned an unreadable answer: {text[:300]}", raw=text)
        if not isinstance(data, dict):
            return cls.failure(f"Explorer returned an unexpected answer: {text[:300]}", raw=text)

        candidates: list[ExplorerCandidate] = []
        for item in data.get("candidates") or []:
            if not isinstance(item, dict):
                continue
            coords = item.get("coords")
            if not is_valid_norm_point(coords):
                logger.warning(f"Dropping Explorer candidate with invalid coords: {item}")
                continue
            label = str(item.get("label") or default_label or f"C{len(candidates) + 1}")
            description = str(item.get("description") or label)
            candidates.append(
                ExplorerCandidate(
                    label=label,
                    coords=(int(coords[0]), int(coords[1])),
                    description=description,
                    bounds=_parse_norm_bounds(item.get("bounds")),
                    source=str(item.get("source") or ""),
                )
            )
        return cls(
            candidates=candidates,
            message=str(data.get("fallback_message") or ""),
            raw=text,
        )


@dataclass(frozen=True)
class RegisteredCandidate:
    """A candidate after registration in the agent's indexed element list."""

    index: int
    pixel: tuple[int, int]
    coords: tuple[int, int]
    description: str
    #: Pixel bounds when known (from the Explorer or from the reused element).
    bounds: tuple[int, int, int, int] | None = None
    #: True when the candidate mapped onto an element that was already indexed.
    reused: bool = False


#: A bare-point candidate this close to an existing indexed center (in
#: pixels) is the same element; scaled with the screen but never tiny.
DEDUP_RADIUS_RATIO = 0.02
DEDUP_RADIUS_MIN_PX = 20


def _find_existing_element(
    indexed_elements: list[dict[str, Any]], pixel: tuple[int, int], width: int
) -> dict[str, Any] | None:
    """Returns the already-indexed element the pixel point belongs to, if any.

    An element with bounds must contain the point; an element known only by
    its center (earlier Explorer candidates) must lie within the dedup radius.
    """
    x, y = pixel
    hit, _source = find_element_at_point(indexed_elements, x, y)
    if hit is not None:
        return hit
    radius = max(DEDUP_RADIUS_MIN_PX, int(round(width * DEDUP_RADIUS_RATIO)))
    best: tuple[float, dict[str, Any]] | None = None
    for element in indexed_elements:
        if not isinstance(element, dict) or element.get("bounds"):
            continue
        center = element.get("center")
        if not (isinstance(center, (list, tuple)) and len(center) == 2):
            continue
        try:
            distance = ((int(center[0]) - x) ** 2 + (int(center[1]) - y) ** 2) ** 0.5
        except (TypeError, ValueError):
            continue
        if distance <= radius and (best is None or distance < best[0]):
            best = (distance, element)
    return best[1] if best else None


def _element_pixel_bounds(element: dict[str, Any]) -> tuple[int, int, int, int] | None:
    bounds = element.get("bounds")
    if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        try:
            return (int(bounds[0]), int(bounds[1]), int(bounds[2]), int(bounds[3]))
        except (TypeError, ValueError):
            return None
    return None


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


async def locate(
    ctx: ArtemisContext,
    state: State,
    query: str,
    context_feedback: str = "",
    *,
    agent_name: str = "operator",
    version: ExplorerVersion | None = None,
) -> ExplorerOutcome:
    """Runs the configured Explorer tier for ``query`` on the latest screenshot.

    ``version`` is a programmatic pin (tests, replay); calling agents never
    provide it -- the tier comes from the user's configuration.
    """
    screenshot_path = getattr(state, "latest_screenshot", None)
    if not screenshot_path:
        return ExplorerOutcome.failure("No screenshot is available for the current screen yet.")

    tier = get_tier(
        resolve_explorer_version(ctx, explicit_version=version, agent_or_profile_name=agent_name)
    )
    logger.info(f"ask_explorer [{agent_name}] tier={tier.name} query={query!r}")

    try:
        raw = await Explorer(ctx).run(
            query,
            context_feedback,
            screenshot_path,
            state,
            version=tier.name,
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error(f"Explorer run failed [{tier.name}]: {e}")
        return ExplorerOutcome.failure(f"Explorer failed: {e}")

    return ExplorerOutcome.from_raw(raw, default_label=query)


def register_candidates(
    ctx: ArtemisContext, state: State, outcome: ExplorerOutcome
) -> list[RegisteredCandidate]:
    """Appends the outcome's candidates to ``state``'s indexed element list."""
    if not outcome.found:
        return []

    indexed_points = getattr(state, "indexed_points", None)
    if indexed_points is None:
        indexed_points = []
        state.indexed_points = indexed_points
    indexed_elements = getattr(state, "indexed_elements", None)
    if indexed_elements is None:
        indexed_elements = []
        state.indexed_elements = indexed_elements

    width, height = resolve_screen_size(ctx, state)
    registered: list[RegisteredCandidate] = []
    for cand in outcome.candidates:
        pixel = norm_to_pixel(cand.coords[0], cand.coords[1], width, height)

        existing = _find_existing_element(indexed_elements, pixel, width)
        if existing is not None:
            index = int(existing.get("index") or (indexed_elements.index(existing) + 1))
            registered.append(
                RegisteredCandidate(
                    index=index,
                    pixel=pixel,
                    coords=cand.coords,
                    description=cand.description,
                    bounds=_element_pixel_bounds(existing),
                    reused=True,
                )
            )
            continue

        pixel_bounds = None
        if cand.bounds is not None:
            left, top = norm_to_pixel(cand.bounds[0], cand.bounds[1], width, height)
            right, bottom = norm_to_pixel(cand.bounds[2], cand.bounds[3], width, height)
            pixel_bounds = (left, top, right, bottom)

        indexed_points.append([pixel[0], pixel[1]])
        index = len(indexed_points)
        indexed_elements.append(
            {
                "index": index,
                "center": [pixel[0], pixel[1]],
                "text": cand.description,
                "bounds": list(pixel_bounds) if pixel_bounds else None,
                "class": "ExplorerCandidate",
                "resource_id": None,
                "is_ocr": cand.source == "ocr",
            }
        )
        registered.append(
            RegisteredCandidate(
                index=index,
                pixel=pixel,
                coords=cand.coords,
                description=cand.description,
                bounds=pixel_bounds,
            )
        )
    return registered


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #


def render_text(query: str, outcome: ExplorerOutcome, registered: list[RegisteredCandidate]) -> str:
    """Plain-text answer for the calling agent."""
    if registered:
        lines = [
            f"Explorer located {len(registered)} candidate(s) for '{query}' in the"
            " indexed element list:"
        ]
        for c in registered:
            line = f"- [{c.index}] '{c.description}' at normalized [{c.coords[0]}, {c.coords[1]}]"
            if c.reused:
                line += " (already in your indexed list)"
            lines.append(line)
        lines.append(
            "Act on a candidate by its index (e.g. target=[index]) or by the"
            " normalized coordinate. Candidates are ranked by confidence."
        )
        if outcome.message:
            lines.append(f"Explorer notes: {outcome.message}")
        return "\n".join(lines)

    if outcome.error:
        return f"Explorer could not run for '{query}'. {outcome.message}"
    detail = outcome.message or "It gave no further detail."
    return (
        f"Explorer could not locate '{query}'. {detail}\n"
        "Consider describing the target differently (visible text, shape, color,"
        " position) or checking whether it is actually visible on this screen."
    )


def render_annotated_image(
    ctx: ArtemisContext, screenshot_path: str, registered: list[RegisteredCandidate]
) -> str | None:
    """Draws the newly registered candidates on the screenshot; returns the path."""
    if not registered:
        return None
    base_dir = (
        Path(ctx.data_engine.base_dir)
        if getattr(ctx, "data_engine", None) and getattr(ctx.data_engine, "base_dir", None)
        else Path(settings.TRACES_PATH)
    )
    explorer_dir = base_dir / "images" / "explorer_tool"
    explorer_dir.mkdir(parents=True, exist_ok=True)

    max_seq = 0
    for existing in glob.glob(str(explorer_dir / "explorer_output_*.jpg")):
        match = re.search(r"_(\d+)\.jpg$", existing)
        if match:
            max_seq = max(max_seq, int(match.group(1)))
    output_path = explorer_dir / f"explorer_output_{max_seq + 1}.jpg"

    try:
        draw_dots(
            screenshot_path,
            [list(c.pixel) for c in registered],
            [str(c.index) for c in registered],
            str(output_path),
            color=CANDIDATE_DOT_COLOR,
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error(f"Failed to annotate explorer candidates: {e}")
        return None
    return str(output_path)


def render_operator_blocks(
    ctx: ArtemisContext,
    state: State,
    query: str,
    outcome: ExplorerOutcome,
    registered: list[RegisteredCandidate],
) -> str | list[dict[str, Any]]:
    """Multimodal answer for the Operator: text plus the annotated screenshot."""
    text = render_text(query, outcome, registered)
    screenshot_path = getattr(state, "latest_screenshot", None)
    image_path = (
        render_annotated_image(ctx, screenshot_path, registered) if screenshot_path else None
    )
    if not image_path:
        return text
    try:
        img_b64 = base64.b64encode(Path(image_path).read_bytes()).decode("utf-8")
    except OSError as e:
        logger.error(f"Failed to read annotated explorer image: {e}")
        return text
    return [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
    ]


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


async def ask_explorer_text(
    ctx: ArtemisContext,
    state: State,
    query: str,
    context_feedback: str = "",
    *,
    agent_name: str = "validator",
) -> str:
    """Text-only entry used by the Validator / Flash runner and MCP executors."""
    outcome = await locate(ctx, state, query, context_feedback, agent_name=agent_name)
    registered = register_candidates(ctx, state, outcome)
    return render_text(query, outcome, registered)


class AskExplorerTool(ArtemisTool):
    """``ask_explorer`` as an :class:`ArtemisTool` (LangChain / GenAI / MCP export)."""

    def __init__(
        self,
        version: ExplorerVersion | None = None,
        agent_name: str = "operator",
        description: str | None = None,
        category: ToolCategory = "explorer",
    ):
        self.version = version
        self.agent_name = agent_name
        super().__init__(
            name=ASK_EXPLORER_TOOL_NAME,
            description=description or ASK_EXPLORER_DESCRIPTION,
            args_schema=AskExplorerArgs,
            category=category,
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        query: str = "",
        context_feedback: str = "",
        tool_call_id: str | None = None,  # pylint: disable=unused-argument
        state: State | None = None,
        version: ExplorerVersion | None = None,
        **kwargs: Any,
    ) -> Any:
        q = query or kwargs.get("Query") or ""
        cf = context_feedback or kwargs.get("ContextFeedback") or ""
        outcome = await locate(
            ctx,
            state,
            q,
            cf,
            agent_name=self.agent_name,
            version=version or self.version,
        )
        registered = register_candidates(ctx, state, outcome)
        return render_operator_blocks(ctx, state, q, outcome, registered)


ask_explorer = AskExplorerTool()
ToolRegistry.register(ask_explorer)


def get_ask_explorer_tool(
    ctx: ArtemisContext,
    version: ExplorerVersion | None = None,
    agent_name: str = "operator",
) -> BaseTool:
    """Exports ``ask_explorer`` as a LangChain tool bound to ``ctx``."""
    return AskExplorerTool(version=version, agent_name=agent_name).to_langchain_tool(ctx)


ask_explorer_wrapper = ToolWrapper(
    tool_fn_getter=get_ask_explorer_tool,
    on_success_fn=lambda output: output,
    on_failure_fn=lambda error: f"Explorer failed: {error}",
)


async def _run_explorer_logic(
    ctx: ArtemisContext,
    state: State,
    query: str,
    context_feedback: str = "",
    version: ExplorerVersion | None = None,
) -> Any:
    """Replay-compatible entry: full pipeline with the Operator presentation."""
    outcome = await locate(ctx, state, query, context_feedback, version=version)
    registered = register_candidates(ctx, state, outcome)
    return render_operator_blocks(ctx, state, query, outcome, registered)
