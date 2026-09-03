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

"""Pixel-level Safety Net: VLM comparison of the target crop before execution.

Validates that the visual target button/component at the action coordinates is
still present, visible, and interactive on the live screen compared to the
original (decision-time) screenshot.

Extracted from ``validator.py``; the public entry point remains the
``ValidatorNode`` method, which delegates here. The ``get_llm_fn`` dependency
is injected by the facade so that tests patching
``artemis.agents.validator.validator.get_llm`` keep working.
"""

import asyncio
import base64
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from artemis.agents.validator.categories import ValidationErrorCategory
from artemis.context import ArtemisContext
from artemis.graph.state import State
from artemis.services.llm import acomplete_structured
from artemis.utils import visualization
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

# Only interactive, element-focused actions are subject to pixel validation.
PIXEL_VALIDATED_ACTIONS = (
    "tap",
    "long_press_on",
    "focus_and_input_text",
    "input_text",
    "click_coordinate",
    "click",
    "long_press",
)

_MAX_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 0.3


def _init_llm_and_prompt(ctx: ArtemisContext, get_llm_fn):
    """Resolves the VLM and loads the prompt template (once per validation)."""
    try:
        llm = get_llm_fn(ctx, name="validator_pixel_safety_net")
    except Exception:
        llm = get_llm_fn(ctx, name="validator")

    prompt_path = Path(__file__).parent.joinpath("pixel_safety_net.md")
    prompt = prompt_path.read_text(encoding="utf-8")
    return llm, prompt


def _build_messages(
    prompt: str,
    orig_crop_bytes: bytes,
    live_crop_bytes: bytes,
    action_name,
    state: State | None,
) -> list:
    """Formulates the multi-modal verification message pair."""
    orig_b64 = base64.b64encode(orig_crop_bytes).decode("utf-8")
    live_b64 = base64.b64encode(live_crop_bytes).decode("utf-8")

    user_content = [
        {"type": "text", "text": "[Image 1 (Reference)]"},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{orig_b64}"},
        },
        {"type": "text", "text": "[Image 2 (Current State)]"},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{live_b64}"},
        },
    ]
    if state:
        thoughts = []
        native_thought = getattr(state, "operator_native_thinking", None)
        if native_thought and native_thought.strip():
            thoughts.append(native_thought.strip())
        raw_thought = getattr(state, "operator_raw_thinking", None)
        if raw_thought and raw_thought.strip():
            thoughts.append(raw_thought.strip())

        operator_thought = "\n\n".join(thoughts)
        if operator_thought.strip():
            context_text = (
                "[Planned Action & Original Thinking]\nAction:"
                f" {action_name}\nOriginal"
                f" Thinking:\n{operator_thought.strip()}"
            )
            user_content.append({"type": "text", "text": f"\n{context_text.strip()}"})

    return [
        SystemMessage(content=prompt),
        HumanMessage(content=user_content),
    ]


async def _run_attempt(
    session,
    llm,
    prompt: str,
    orig_crop_bytes: bytes,
    current_coords,
    action_name,
    state: State | None,
    attempt: int,
) -> tuple[bool, ValidationErrorCategory, str] | None:
    """Runs one pixel validation attempt.

    Returns a final (passed, category, reason) verdict, or None when the
    attempt yielded no verdict (never happens today: every branch is final).
    Raises on infrastructure errors so the caller's retry loop can decide.
    """
    # A. Take fresh live screenshot (captures state changes / dynamic loading settling)
    live_screenshot_b64 = None
    try:
        live_screenshot_b64 = await session.screenshot_b64()
        success_img = True
    except Exception:
        success_img, live_screenshot_b64 = False, None

    if not success_img or not live_screenshot_b64:
        raise Exception("Failed to acquire live screenshot from controller/MCP tool.")

    # B. Decode and crop the live screen target
    live_bytes = base64.b64decode(live_screenshot_b64)
    live_crop_bytes = visualization.crop_and_annotate_target(
        live_bytes, current_coords, crop_size=None, dot_radius=15
    )

    # C. Formulate multi-modal verification message
    messages = _build_messages(prompt, orig_crop_bytes, live_crop_bytes, action_name, state)

    # D. Invoke Universal VLM. Parsing (fences, repair) and one
    # corrective re-ask on unparseable JSON are handled by the
    # structured-output layer; a StructuredOutputError lands in
    # the attempt loop's except like any other attempt failure.
    res_json = await acomplete_structured(llm, messages)
    if not isinstance(res_json, dict):
        raise ValueError(
            "Pixel validation response parsed to"
            f" {type(res_json).__name__}, expected a JSON object."
        )
    reasoning = res_json.get("reasoning", "")
    is_present = res_json.get("is_present", True)
    confidence = res_json.get("confidence", 1.0)

    logger.info(
        f"Pixel validation attempt {attempt}/{_MAX_ATTEMPTS} result:"
        f" is_present={is_present}, confidence={confidence:.2f}, reasoning={reasoning}"
    )

    # E. Branching based on outcome
    if is_present:
        logger.info(f"Pixel validation SUCCESS on attempt {attempt}/{_MAX_ATTEMPTS}.")
        return True, ValidationErrorCategory.NONE, ""

    # Target not present: fail or bypass based on confidence without retry!
    if confidence >= 0.7:
        reason_msg = (
            f"Pixel-level validation failed: {reasoning}"
            if reasoning
            else (
                "Pixel-level validation failed: The target UI"
                " element at the exact coordinates has disappeared,"
                " changed, or is blocked."
            )
        )
        return (
            False,
            ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED,
            reason_msg,
        )

    logger.warning(
        f"Pixel validation failed but confidence ({confidence:.2f}) is below 0.7. Bypassing check."
    )
    return True, ValidationErrorCategory.PIXEL_BYPASSED, ""


async def validate_action_precondition_pixel(
    ctx: ArtemisContext,
    session,
    action_item: dict,
    pre_screenshot_b64: str,
    original_coords: list[int] | None = None,
    state: State | None = None,
    *,
    get_llm_fn,
) -> tuple[bool, ValidationErrorCategory, str]:
    """Pixel-level Safety Net entry point (see module docstring)."""
    action_name = action_item.get("action")

    # 1. White-list filtering: Only validate interactive, element-focused actions
    if action_name not in PIXEL_VALIDATED_ACTIONS:
        return True, ValidationErrorCategory.PIXEL_BYPASSED, ""

    current_coords = action_item.get("coordinates")
    orig_coords = original_coords or current_coords

    if not orig_coords or len(orig_coords) != 2:
        logger.info("Pixel safety net skipped: no valid coordinates.")
        return True, ValidationErrorCategory.PIXEL_BYPASSED, ""

    if not current_coords or len(current_coords) != 2:
        current_coords = orig_coords

    logger.info(
        "Pre-execution pixel validation: checking target position"
        f" {orig_coords} in original image vs {current_coords} on live"
        " screen..."
    )

    # 2. Decode and crop the static reference (original) image ONCE outside the loop to save CPU
    try:
        orig_bytes = base64.b64decode(pre_screenshot_b64)
        orig_crop_bytes = visualization.crop_and_annotate_target(
            orig_bytes, orig_coords, crop_size=None, dot_radius=15
        )
    except Exception as e:
        logger.error(f"Failed to prepare original crop for pixel safety net: {e}. Bypassing check.")
        return True, ValidationErrorCategory.PIXEL_BYPASSED, ""

    # 3. Get LLM Configuration once outside the loop
    try:
        llm, prompt = _init_llm_and_prompt(ctx, get_llm_fn)
    except Exception as e:
        logger.error(f"Failed to initialize LLM/prompt for pixel safety net: {e}. Bypassing check.")
        return True, ValidationErrorCategory.PIXEL_BYPASSED, ""

    # 4. Start high-precision execution validation retry loop
    last_error = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        logger.info(f"Pre-execution pixel validation: Attempt {attempt}/{_MAX_ATTEMPTS}...")
        try:
            verdict = await _run_attempt(
                session,
                llm,
                prompt,
                orig_crop_bytes,
                current_coords,
                action_name,
                state,
                attempt,
            )
            if verdict is not None:
                return verdict
        except Exception as attempt_err:
            logger.warning(
                f"Error during pixel validation attempt {attempt}/{_MAX_ATTEMPTS}: {attempt_err}"
            )
            last_error = attempt_err
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
                continue
            logger.error(
                "Pixel validation repeatedly failed with errors. Last"
                f" error: {last_error}. Bypassing check to avoid"
                " blocking flow."
            )
            return True, ValidationErrorCategory.PIXEL_BYPASSED, ""

    return True, ValidationErrorCategory.PIXEL_BYPASSED, ""
