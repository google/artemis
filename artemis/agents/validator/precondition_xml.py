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

"""XML-hierarchy Safety Net: pre-execution validation of the action target.

Checks that the element the Operator decided to interact with is still present
on the live screen (via the UI hierarchy) and substantially consistent, with
coordinate self-healing for small drifts and a structured failure taxonomy
(shifted / occupied / disappeared) consumed by the FailureAnalyzer.

Extracted from ``validator.py``; the public entry points remain the
``ValidatorNode`` methods, which delegate here.
"""

import asyncio
import difflib
import math
import re

from artemis.agents.validator.failure_analyzer import ValidationErrorCategory
from artemis.constants import VALIDATOR_UI_HIERARCHY_TIMEOUT
from artemis.context import ArtemisContext
from artemis.graph.state import State
from artemis.utils import visualization
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

# Only interactive, element-focused actions are subject to XML validation.
XML_VALIDATED_ACTIONS = (
    "tap",
    "long_press_on",
    "focus_and_input_text",
    "focus_and_clear_text",
)

# Signal weights for candidate scoring.
W_ID = 0.5
W_TEXT = 0.4
W_BOUNDS = 0.3
W_COORD = 0.3


async def validate_action_precondition(
    node, session, action_item: dict, state: State | None = None
) -> tuple[bool, ValidationErrorCategory, str]:
    """Retry wrapper around the single-shot XML precondition validation.

    A short retry loop accommodates screen transitions and loading delays.
    ``node`` is the ValidatorNode; the single-shot check is dispatched through
    it so tests can patch the method.
    """
    max_pre_attempts = 3
    pre_retry_delay = 0.4

    passed = False
    category = ValidationErrorCategory.NONE
    reason = ""

    for pre_attempt in range(1, max_pre_attempts + 1):
        passed, category, reason = await node._validate_action_precondition_single(
            session, action_item, state
        )
        if passed:
            return True, category, reason

        if category == ValidationErrorCategory.XML_BYPASSED:
            # Do not retry on XML bypass/timeout; return immediately
            # so it falls back to Pixel-based VLM validation
            return False, category, reason

        if pre_attempt < max_pre_attempts:
            logger.info(
                "Pre-execution validation failed on attempt"
                f" {pre_attempt}/{max_pre_attempts}: {reason}. Retrying in"
                f" {pre_retry_delay}s..."
            )
            await asyncio.sleep(pre_retry_delay)
        else:
            break

    return passed, category, reason


def _resolve_screen_dims(ctx: ArtemisContext, state: State | None) -> tuple[int, int]:
    """Resolves the screen dimensions the Operator's coordinates refer to."""
    width = 1080
    height = 2400
    operator_raw_data = getattr(state, "operator_raw_data", {}) or {}
    w_raw = operator_raw_data.get("width")
    h_raw = operator_raw_data.get("height")
    if isinstance(w_raw, int) and isinstance(h_raw, int):
        width = w_raw
        height = h_raw
    else:
        device = getattr(ctx, "device", None)
        if device:
            width = getattr(device, "device_width", 1080)
            height = getattr(device, "device_height", 2400)
    return width, height


async def _fetch_live_elements(session):
    """Pulls the live XML tree with a strict timeout.

    Returns ``(elements, None)`` on success, or ``(None, (False, category,
    reason))`` when the check should be bypassed to Pixel-based validation.
    """
    try:
        try:
            # The timeout must ride inside the session (MCP read timeout), never an
            # outer asyncio.wait_for: cancelling call_tool mid-flight corrupts the
            # in-memory transport and bricks the session for all later callers.
            elements = await session.ui_hierarchy(timeout=VALIDATOR_UI_HIERARCHY_TIMEOUT)
        except Exception as e:
            logger.warning(
                f"Failed to get live XML via MCP: {e!r}. Falling back to"
                " Pixel-based validation."
            )
            return None, (
                False,
                ValidationErrorCategory.XML_BYPASSED,
                f"XML hierarchy fetch timed out or errored: {e!r}",
            )

        if not elements or not isinstance(elements, list):
            logger.warning(
                "Live hierarchy empty or invalid. Falling back to Pixel-based validation."
            )
            return None, (
                False,
                ValidationErrorCategory.XML_BYPASSED,
                "Live hierarchy empty or invalid.",
            )

    except Exception as e:
        logger.error(
            f"Unexpected error during live XML fetch: {e}. Falling back to"
            " Pixel-based validation."
        )
        return None, (
            False,
            ValidationErrorCategory.XML_BYPASSED,
            f"Unexpected error during live XML fetch: {e}",
        )

    return elements, None


def _normalize_text(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"\(\d+\+?\)", "", s)
    s = re.sub(r"\[\d+\+?\]", "", s)
    s = re.sub(r"[^\w\s\d]", "", s)
    return s.strip()


def _aggregate_text(elem: dict) -> str:
    """Recursively aggregates text from a candidate container node."""
    text = elem.get("text") or elem.get("content-desc") or elem.get("accessibilityText") or ""
    if not isinstance(text, str):
        text = str(text)
    children = elem.get("children") or []
    if children:
        for child in children:
            if isinstance(child, dict):
                text += " " + _aggregate_text(child)
    return text


def _text_similarity(norm_target_text: str, elem_norm_text: str) -> float:
    """Scores text similarity between normalized target and element text."""
    if not (norm_target_text and elem_norm_text):
        return 0.0
    if norm_target_text == elem_norm_text:
        return 1.0
    if norm_target_text in elem_norm_text or elem_norm_text in norm_target_text:
        return 0.8
    return difflib.SequenceMatcher(None, norm_target_text, elem_norm_text).ratio()


def _bounds_match(target_bounds, elem_bounds) -> tuple[float, bool]:
    """Returns (IoU bounds score, size-mismatch flag) for the bounds pair."""
    bounds_score = 0.0
    size_mismatch = False
    if target_bounds and elem_bounds and len(target_bounds) == 4 and len(elem_bounds) == 4:
        l1, t1, r1, b1 = target_bounds
        l2, t2, r2, b2 = elem_bounds

        # Check size ratio mismatch to prevent matching small widget to giant container
        w1, h1 = r1 - l1, b1 - t1
        w2, h2 = r2 - l2, b2 - t2
        if w1 > 0 and h1 > 0 and w2 > 0 and h2 > 0:
            size_ratio_w = max(w1, w2) / min(w1, w2)
            size_ratio_h = max(h1, h2) / min(h1, h2)
            if size_ratio_w > 2.5 or size_ratio_h > 2.5:
                size_mismatch = True

        li = max(l1, l2)
        ti = max(t1, t2)
        ri = min(r1, r2)
        bi = min(b1, b2)
        if ri > li and bi > ti:
            inter_area = (ri - li) * (bi - ti)
            union_area = (r1 - l1) * (b1 - t1) + (r2 - l2) * (b2 - t2) - inter_area
            if union_area > 0:
                bounds_score = inter_area / union_area

    return bounds_score, size_mismatch


def _combine_signals(
    *,
    target_resource_id,
    target_text,
    target_bounds,
    original_coords,
    id_match: bool,
    elem_res_id: str,
    norm_target_text: str,
    elem_norm_text: str,
    text_score: float,
    bounds_score: float,
    contains_coord: bool,
) -> tuple[float, float]:
    """Combines the weighted match signals into (score, identity_score)."""
    signals = []
    weights = []
    identity_signals = []
    identity_weights = []

    # 1. Resource ID Signal
    if target_resource_id:
        weights.append(W_ID)
        identity_weights.append(W_ID)
        if id_match:
            signals.append(W_ID * 1.0)
            identity_signals.append(W_ID * 1.0)
        elif not elem_res_id:
            signals.append(W_ID * 0.3)  # Soft penalty for missing ID
            identity_signals.append(W_ID * 0.3)
        else:
            signals.append(W_ID * -0.5)  # Hard penalty for mismatched ID
            identity_signals.append(W_ID * -0.5)

    # 2. Text Signal
    if target_text:
        weights.append(W_TEXT)
        identity_weights.append(W_TEXT)
        if norm_target_text and elem_norm_text:
            signals.append(W_TEXT * text_score)
            identity_signals.append(W_TEXT * text_score)
        elif not elem_norm_text:
            signals.append(W_TEXT * 0.0)  # Penalty for missing expected text
            identity_signals.append(W_TEXT * 0.0)

    # 3. Bounds Signal
    if target_bounds:
        weights.append(W_BOUNDS)
        signals.append(W_BOUNDS * bounds_score)

    # 4. Coordinate Containment Signal
    if original_coords:
        weights.append(W_COORD)
        signals.append(W_COORD * (1.0 if contains_coord else 0.0))

    score = sum(signals) / sum(weights) if weights else 1.0
    identity_score = (
        sum(identity_signals) / sum(identity_weights) if identity_weights else 1.0
    )
    return score, identity_score


def _score_element(
    elem: dict,
    action_item: dict,
    *,
    target_text,
    target_bounds,
    target_resource_id,
    norm_target_text: str,
) -> dict | None:
    """Scores one hierarchy element against the action target metadata.

    Returns a candidate dict, or None if the element has no usable bounds.
    """
    elem_bounds_str = elem.get("bounds")
    elem_bounds = visualization.parse_bounds(elem_bounds_str)
    if not elem_bounds:
        return None

    elem_raw_text = _aggregate_text(elem)
    elem_norm_text = _normalize_text(elem_raw_text)
    elem_res_id = elem.get("resource-id") or elem.get("resourceId") or ""

    elem_cx, elem_cy = visualization.get_center_coordinates(*elem_bounds)

    original_coords = action_item.get("coordinates") or [0, 0]
    dist = math.sqrt((elem_cx - original_coords[0]) ** 2 + (elem_cy - original_coords[1]) ** 2)

    id_match = target_resource_id and elem_res_id and target_resource_id == elem_res_id

    text_score = _text_similarity(norm_target_text, elem_norm_text)
    bounds_score, size_mismatch = _bounds_match(target_bounds, elem_bounds)

    # Calculate Coordinate Containment
    contains_coord = False
    if original_coords and len(original_coords) == 2 and elem_bounds and len(elem_bounds) == 4:
        cx, cy = original_coords
        l2, t2, r2, b2 = elem_bounds
        contains_coord = l2 <= cx <= r2 and t2 <= cy <= b2

    # Apply size mismatch penalty
    if size_mismatch and not id_match:
        text_score = 0.0
        bounds_score = 0.0
        contains_coord = False

    score, identity_score = _combine_signals(
        target_resource_id=target_resource_id,
        target_text=target_text,
        target_bounds=target_bounds,
        original_coords=original_coords,
        id_match=id_match,
        elem_res_id=elem_res_id,
        norm_target_text=norm_target_text,
        elem_norm_text=elem_norm_text,
        text_score=text_score,
        bounds_score=bounds_score,
        contains_coord=contains_coord,
    )

    decay = max(0.5, 1.0 - (dist / 800.0))
    score *= decay

    return {
        "element": elem,
        "center": [elem_cx, elem_cy],
        "bounds": elem_bounds,
        "text": elem_raw_text,
        "resource_id": elem_res_id,
        "score": score,
        "identity_score": identity_score,
        "distance": dist,
        "text_score": text_score,
        "id_match": id_match,
    }


def _handle_match_success(
    action_item: dict, best: dict, target_text, scale_factor: float
) -> tuple[bool, ValidationErrorCategory, str]:
    """Handles a passing match: self-heals coordinates for moderate drifts."""
    if best["distance"] <= 200 * scale_factor:
        new_coords = best["center"]
        old_coords = action_item.get("coordinates")
        if old_coords != new_coords:
            logger.success(
                "Pre-execution validation SUCCESS"
                f" (score={best['score']:.2f}). Target element"
                f" '{target_text}' shifted by {best['distance']:.1f}px."
                f" Self-healing: correcting coordinates {old_coords} ->"
                f" {new_coords}."
            )
            action_item["coordinates"] = new_coords
            # Drop the now-stale normalized coordinates so the canonical
            # translation re-derives them from the healed pixel center.
            action_item.pop("normalized_coordinates", None)
    else:
        logger.info(
            "Pre-execution validation SUCCESS"
            f" (score={best['score']:.2f}). Target element matched but"
            f" shifted by {best['distance']:.1f}px"
            f" (>{200 * scale_factor:.1f}px). Bypassing coordinate"
            " self-healing to remain conservative."
        )
    return True, ValidationErrorCategory.NONE, ""


def _is_valid_shift(best: dict, scale_factor: float) -> bool:
    """Applies the strict shift criteria (preferring disappeared over shifted).

    We must be conservative to avoid confusing disappearance as shift. Shift
    misclassification as disappeared is acceptable, but disappeared as shift
    causes errors.
    """
    max_allowed_distance = 100.0 * scale_factor
    if best.get("id_match", False):
        # Allow larger distance threshold only for strong resource ID matches
        max_allowed_distance = 300.0 * scale_factor

    if best["distance"] <= max_allowed_distance:
        if best["identity_score"] >= 0.85:
            # High confidence identity match (e.g. exact text or exact ID)
            return True
        if best["identity_score"] >= 0.5 and best.get("id_match", False):
            # Moderate confidence with exact resource ID match
            return True
    return False


def _element_is_interactive(e: dict) -> bool:
    clickable = e.get("clickable")
    long_clickable = e.get("long-clickable") or e.get("longClickable")
    focusable = e.get("focusable")
    enabled = e.get("enabled")

    def to_bool(v) -> bool:
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        return str(v).lower() == "true"

    return to_bool(clickable) or to_bool(long_clickable) or to_bool(focusable) or to_bool(enabled)


def _find_occupant(elements: list, original_coords) -> tuple | None:
    """Finds the most specific element containing the expected coordinates."""
    occupant = None
    if original_coords and len(original_coords) == 2:
        orig_cx, orig_cy = original_coords
        for elem in elements:
            if not isinstance(elem, dict):
                continue
            elem_bounds_str = elem.get("bounds")
            elem_bounds = visualization.parse_bounds(elem_bounds_str)
            if elem_bounds:
                left, top, right, bottom = elem_bounds
                if left <= orig_cx <= right and top <= orig_cy <= bottom:
                    elem_area = (right - left) * (bottom - top)
                    if not occupant or elem_area < occupant[2]:
                        occupant = (elem, elem_bounds, elem_area)
    return occupant


def _coords_hit_interactive_element(
    elements: list, original_coords, width: int, height: int
) -> bool:
    """True if any non-full-screen interactive element covers the coordinates."""
    if not (original_coords and len(original_coords) == 2):
        return False
    orig_cx, orig_cy = original_coords
    for e in elements:
        if not isinstance(e, dict):
            continue
        eb_str = e.get("bounds")
        eb = visualization.parse_bounds(eb_str)
        if eb:
            e_left, e_top, e_right, e_bottom = eb
            if e_left <= orig_cx <= e_right and e_top <= orig_cy <= e_bottom:
                # Exclude full-screen root background elements
                e_width = e_right - e_left
                e_height = e_bottom - e_top
                is_full_screen = e_width >= width - 10 and e_height >= height - 10

                if _element_is_interactive(e) and not is_full_screen:
                    return True
    return False


def _describe_occupation(
    elements: list,
    occupant: tuple | None,
    original_coords,
    target_bounds,
    width: int,
    height: int,
) -> tuple[bool, str, list | None]:
    """Decides whether the expected coordinates are occupied by another element.

    Returns (is_occupied, occupant_desc, occupant_bounds).
    """
    is_occupied = False
    occupant_desc = "anonymous element"
    occupant_bounds = None

    if occupant:
        elem, elem_bounds, _ = occupant
        occupant_text = (
            elem.get("text") or elem.get("content-desc") or elem.get("accessibilityText") or ""
        )
        occupant_res_id = elem.get("resource-id") or elem.get("resourceId") or ""
        occupant_bounds = elem_bounds

        # Check if the occupant has content
        has_content = bool(occupant_text or occupant_res_id)

        # Check if this occupant or any container covering this point is interactive
        coords_interactive = _coords_hit_interactive_element(
            elements, original_coords, width, height
        )

        # Exclude full-screen or giant parent containers from being considered blockers
        is_occupant_full_screen = False
        if occupant_bounds:
            o_w = occupant_bounds[2] - occupant_bounds[0]
            o_h = occupant_bounds[3] - occupant_bounds[1]
            is_occupant_full_screen = o_w >= width - 10 and o_h >= height - 10

            # If target bounds are known, check if occupant is a giant container
            if target_bounds:
                t_w = target_bounds[2] - target_bounds[0]
                t_h = target_bounds[3] - target_bounds[1]
                t_area = t_w * t_h
                o_area = o_w * o_h
                if t_area > 0 and o_area > 3.0 * t_area:
                    is_occupant_full_screen = True

        if (has_content or coords_interactive) and not is_occupant_full_screen:
            is_occupied = True
            occupant_desc = (
                f"'{occupant_text}' ({occupant_res_id})"
                if has_content
                else "interactive anonymous element"
            )

    return is_occupied, occupant_desc, occupant_bounds


def _classify_failure(
    elements: list,
    action_item: dict,
    best: dict,
    *,
    target_text,
    target_bounds,
    target_resource_id,
    width: int,
    height: int,
    scale_factor: float,
) -> tuple[bool, ValidationErrorCategory, str]:
    """Classifies a below-threshold match as shifted / occupied / disappeared."""
    original_coords = action_item.get("coordinates")

    # 1. Shifted Case
    if _is_valid_shift(best, scale_factor):
        reason = (
            f"Target element '{target_text}' ({target_resource_id})"
            f" expected at {original_coords} (bounds: {target_bounds})"
            f" has shifted.\n- New location: {best['center']} (bounds:"
            f" {best['bounds']})"
        )
        return False, ValidationErrorCategory.TARGET_SHIFTED, reason

    # 2. Occupied Case
    occupant = _find_occupant(elements, original_coords)
    is_occupied, occupant_desc, occupant_bounds = _describe_occupation(
        elements, occupant, original_coords, target_bounds, width, height
    )
    if is_occupied:
        reason = (
            f"The expected position {original_coords} for"
            f" '{target_text}' ({target_resource_id}) is"
            " occupied/intercepted by a different element:"
            f" {occupant_desc} at bounds {occupant_bounds}."
        )
        return False, ValidationErrorCategory.TARGET_OCCUPIED, reason

    # 3. Completely Missing Case
    reason = (
        f"Target element '{target_text}' ({target_resource_id})"
        f" expected at {original_coords} (bounds:"
        f" {target_bounds}) was not found on the screen."
    )
    return False, ValidationErrorCategory.TARGET_DISAPPEARED, reason


async def validate_action_precondition_single(
    ctx: ArtemisContext, session, action_item: dict, state: State | None = None
) -> tuple[bool, ValidationErrorCategory, str]:
    """Single-shot Safety Net check against the live XML hierarchy.

    Returns (True, ValidationErrorCategory.NONE, "") if validation passes or
    is skipped. Returns (False, category, error_msg) if validation fails.
    """
    action_name = action_item.get("action")

    # 1. White-list filtering: Only validate interactive, element-focused actions
    if action_name not in XML_VALIDATED_ACTIONS:
        return True, ValidationErrorCategory.NONE, ""

    target_text = action_item.get("target_text")
    target_bounds = action_item.get("target_bounds")  # [l, t, r, b]
    target_resource_id = action_item.get("target_resource_id")

    width, height = _resolve_screen_dims(ctx, state)

    # Calculate diagonal-based scale factor for resolution-independent thresholds
    ref_diagonal = math.sqrt(1080**2 + 2400**2)  # ~2631.8
    current_diagonal = math.sqrt(width**2 + height**2)
    scale_factor = current_diagonal / ref_diagonal

    # 2. Skip validation if there's no target metadata to match against
    if not target_text and not target_bounds and not target_resource_id:
        return True, ValidationErrorCategory.NONE, ""

    logger.info(
        f"Pre-execution validation: checking target '{target_text}'"
        f" ({target_resource_id}) at {target_bounds}..."
    )

    # 3. Pull live XML tree with strict timeout to avoid hanging the execution flow
    elements, bypass = await _fetch_live_elements(session)
    if bypass is not None:
        return bypass

    # 4. Matching Heuristics & Scoring
    norm_target_text = _normalize_text(target_text)

    candidates = []
    for elem in elements:
        if not isinstance(elem, dict):
            continue
        candidate = _score_element(
            elem,
            action_item,
            target_text=target_text,
            target_bounds=target_bounds,
            target_resource_id=target_resource_id,
            norm_target_text=norm_target_text,
        )
        if candidate is not None:
            candidates.append(candidate)

    if not candidates:
        logger.warning(
            "No candidates with valid bounds found. Falling back to Pixel-based validation."
        )
        return (
            False,
            ValidationErrorCategory.XML_BYPASSED,
            "No candidates with valid bounds found.",
        )

    candidates.sort(key=lambda x: x["score"], reverse=True)
    best = candidates[0]

    threshold = 0.55 if best["distance"] <= 150 * scale_factor else 0.75

    if best["score"] >= threshold:
        return _handle_match_success(action_item, best, target_text, scale_factor)

    return _classify_failure(
        elements,
        action_item,
        best,
        target_text=target_text,
        target_bounds=target_bounds,
        target_resource_id=target_resource_id,
        width=width,
        height=height,
        scale_factor=scale_factor,
    )
