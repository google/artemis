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

"""Universal Multi-Model Object Detector for Artemis."""

import asyncio
import base64
import io
import json
import math
import os
from pathlib import Path
import re

from langchain_core.messages import HumanMessage, ToolMessage

from artemis.llm.structured import ParseFailure, parse_structured
from artemis.services.llm import get_llm
from artemis.utils.image_codec import image_data_uri
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

_DETECTOR_SEMAPHORE = asyncio.Semaphore(6)

# The detector's output contract: points are [x, y] on a normalized 0-1000
# grid over the image that was sent (consumers divide by 1000 to get pixels).
# Items may also carry ``box_2d`` corner pairs in the same declared convention
# (Gemma 4's trained detection output is [ymin, xmin, ymax, xmax] / 1000, i.e.
# the default ``yx_1000``); a box is normalized in place to [x1, y1, x2, y2]
# and synthesizes ``point`` from its center when the item lacks one.
# Models differ in what they natively emit; ``coordinate_format`` on the
# resolved endpoint declares the convention so results can be normalized:
#   axis order  — ``yx`` (Gemini ER style) or ``xy`` (most vision models)
#   scale       — ``1000`` grid, ``px`` absolute pixels, ``norm`` 0-1 floats
_DEFAULT_COORDINATE_FORMAT = "yx_1000"
_PIXEL_GRID = 1000.0

# Qwen-family processors smart-resize inputs to a multiple of the patch-merge
# factor within min/max pixel bounds; pixel-space coordinates land on that
# resized grid, not the raw image grid.
_QWEN_RESIZE_FACTOR = 32
_QWEN_MIN_PIXELS = 65536
_QWEN_MAX_PIXELS = 16777216  # size.longest_edge in the shipped preprocessor config


def _image_dimensions(image_data: bytes) -> tuple[int, int] | None:
    """Return (width, height) of encoded image bytes, or None when undecodable."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_data)) as img:
            return img.width, img.height
    except Exception:
        return None


def _qwen_resized_dimensions(width: int, height: int) -> tuple[int, int]:
    """Grid a Qwen-family processor sees after its smart resize."""
    factor = _QWEN_RESIZE_FACTOR
    w_bar = round(width / factor) * factor
    h_bar = round(height / factor) * factor
    if w_bar * h_bar > _QWEN_MAX_PIXELS:
        beta = math.sqrt((width * height) / _QWEN_MAX_PIXELS)
        w_bar = math.floor(width / beta / factor) * factor
        h_bar = math.floor(height / beta / factor) * factor
    elif w_bar * h_bar < _QWEN_MIN_PIXELS:
        beta = math.sqrt(_QWEN_MIN_PIXELS / (width * height))
        w_bar = math.ceil(width * beta / factor) * factor
        h_bar = math.ceil(height * beta / factor) * factor
    return w_bar, h_bar


def _endpoint_coordinate_format(llm) -> str:
    fmt = getattr(getattr(llm, "endpoint", None), "coordinate_format", None)
    if isinstance(fmt, str) and fmt in _COORDINATE_FORMATS:
        return fmt
    return _DEFAULT_COORDINATE_FORMAT


_COORDINATE_FORMATS = frozenset({"yx_1000", "xy_1000", "yx_px", "xy_px", "yx_norm", "xy_norm"})


def _detected_batch_scale(items: list) -> str | None:
    """Infer the actual scale a model used across a whole response.

    The contract is 0-1000, but when a model ignores it the whole batch
    shares one convention: every coordinate fitting in 0-1 means ``norm``;
    any coordinate above 1000 means ``px``. Mixed magnitudes stay on the
    declared grid so sub-1000 corner points are not rescaled twice.
    """
    coords = [
        abs(float(c))
        for item in items
        if isinstance(item, dict)
        for key in ("point", "box_2d")
        if isinstance(item.get(key), (list, tuple))
        and len(item[key]) == (2 if key == "point" else 4)
        for c in item[key]
        if isinstance(c, (int, float)) and not isinstance(c, bool)
    ]
    if not coords:
        return None
    max_coord = max(coords)
    if max_coord <= 1.0 and any(c != 0.0 for c in coords):
        return "norm"
    if max_coord > _PIXEL_GRID:
        return "px"
    return None


def _normalize_detected_point(
    item: dict,
    coordinate_format: str,
    image_size: tuple[int, int] | None,
    batch_scale: str | None,
) -> None:
    """Rewrite ``item['point']`` to the [x, y] 0-1000 contract in place.

    Axis order and scale come from the endpoint's declared coordinate
    format; ``batch_scale`` (``norm``/``px``) overrides the declared scale
    only when the whole response clearly violated the contract.
    """
    point = item.get("point")
    if not (isinstance(point, (list, tuple)) and len(point) == 2):
        return
    try:
        first, second = float(point[0]), float(point[1])
    except (TypeError, ValueError):
        return

    x, y = (second, first) if coordinate_format.startswith("yx") else (first, second)

    scale = batch_scale or coordinate_format.rsplit("_", 1)[-1]
    if scale == "px":
        if image_size:
            grid_w, grid_h = _qwen_resized_dimensions(*image_size)
            x, y = x * _PIXEL_GRID / grid_w, y * _PIXEL_GRID / grid_h
    elif scale == "norm":
        x, y = x * _PIXEL_GRID, y * _PIXEL_GRID

    item["point"] = [x, y]


def _normalize_detected_box(
    item: dict,
    coordinate_format: str,
    image_size: tuple[int, int] | None,
    batch_scale: str | None,
) -> bool:
    """Rewrite ``item['box_2d']`` to [x1, y1, x2, y2] on the 0-1000 contract.

    When the item has no usable ``point``, the box center becomes it. Returns
    True only in that case: a synthesized point is already on the contract
    grid and must not go through ``_normalize_detected_point`` again.
    """
    box = item.get("box_2d")
    if not (isinstance(box, (list, tuple)) and len(box) == 4):
        return False
    try:
        a, b, c, d = (float(v) for v in box)
    except (TypeError, ValueError):
        return False

    x1, y1, x2, y2 = (b, a, d, c) if coordinate_format.startswith("yx") else (a, b, c, d)

    scale = batch_scale or coordinate_format.rsplit("_", 1)[-1]
    if scale == "px":
        if image_size:
            grid_w, grid_h = _qwen_resized_dimensions(*image_size)
            x1, x2 = x1 * _PIXEL_GRID / grid_w, x2 * _PIXEL_GRID / grid_w
            y1, y2 = y1 * _PIXEL_GRID / grid_h, y2 * _PIXEL_GRID / grid_h
    elif scale == "norm":
        x1, y1, x2, y2 = x1 * _PIXEL_GRID, y1 * _PIXEL_GRID, x2 * _PIXEL_GRID, y2 * _PIXEL_GRID

    item["box_2d"] = [x1, y1, x2, y2]

    point = item.get("point")
    if isinstance(point, (list, tuple)) and len(point) == 2:
        try:
            float(point[0])
            float(point[1])
            return False
        except (TypeError, ValueError):
            pass
    item["point"] = [(x1 + x2) / 2, (y1 + y2) / 2]
    return True


async def _detect_single_label(
    llm,
    ctx,
    image_bytes,
    mime_type,
    label,
    templates,
    timeout_val=None,
) -> list[dict]:
    if timeout_val is None:
        timeout_val = float(os.environ.get("OBJECT_DETECTOR_TIMEOUT", "10.0"))

    img_b64 = base64.b64encode(image_bytes).decode("utf-8")

    for i, template in enumerate(templates):
        prompt = template.replace("{labels_str}", label)
        messages = [
            HumanMessage(
                content=[
                    {
                        "type": "image_url",
                        "image_url": {"url": image_data_uri(img_b64)},
                    },
                    {"type": "text", "text": prompt},
                ]
            )
        ]

        try:
            logger.info(f"Calling Object Detector for '{label}' (Attempt {i + 1})...")
            async with _DETECTOR_SEMAPHORE:
                response = await asyncio.wait_for(
                    llm.ainvoke(messages),
                    timeout=timeout_val,
                )

            output = response.content if isinstance(response.content, str) else ""
            if isinstance(response.content, list):
                output = "".join(
                    b.get("text", "")
                    for b in response.content
                    if isinstance(b, dict) and "text" in b
                )
            logger.info(f"Received response for '{label}' (Attempt {i + 1})")

            res_json = parse_structured(output)
            if isinstance(res_json, list):
                valid_results = []
                for item in res_json:
                    if isinstance(item, dict):
                        item["label"] = label
                        valid_results.append(item)
                return valid_results
            if isinstance(res_json, ParseFailure):
                logger.warning(
                    f"Object detector response for '{label}' was not valid"
                    f" JSON (attempt {i + 1}): {res_json.error}"
                )
            else:
                logger.warning(
                    f"Object detector response for '{label}' parsed to"
                    f" {type(res_json).__name__} instead of a list (attempt {i + 1})."
                )

        except Exception as e:
            logger.warning(f"Detection attempt {i + 1} failed for '{label}': {e}")

    return []


async def _run_object_detection(
    ctx,
    image_bytes: bytes | str | Path | None = None,
    queries: list[str] | None = None,
    templates: list[str] | None = None,
    mime_type: str = "image/jpeg",
    global_timeout: float = 30.0,
    image_path: str | Path | None = None,
) -> dict:
    target_img = image_bytes if image_bytes is not None else image_path
    if target_img is None:
        raise ValueError(
            "Either image_bytes or image_path must be provided to _run_object_detection"
        )
    if isinstance(target_img, (str, Path)):
        image_data = Path(target_img).read_bytes()
    else:
        image_data = target_img

    queries = queries or []
    templates = templates or ["Point to the following objects: {labels_str}"]
    try:
        llm = get_llm(ctx, name="object_detector", is_utils=True)
    except Exception:
        llm = get_llm(ctx, name="operator")

    raw_timeout = getattr(getattr(ctx, "llm_config", None), "timeout", None)
    if isinstance(raw_timeout, (int, float)):
        timeout_val = float(raw_timeout)
    else:
        timeout_val = float(os.environ.get("OBJECT_DETECTOR_TIMEOUT", "10.0"))

    tasks = [
        asyncio.create_task(
            _detect_single_label(
                llm,
                ctx,
                image_data,
                mime_type,
                label=query,
                templates=templates,
                timeout_val=timeout_val,
            )
        )
        for query in queries
    ]

    done, pending = await asyncio.wait(tasks, timeout=global_timeout)

    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    fused_results = []
    for task in done:
        try:
            res = task.result()
            if res:
                fused_results.extend(res)
        except Exception as e:
            logger.error(f"Task raised exception: {e}")

    # Normalize model points to the [x, y] 0-1000 contract. Axis order and
    # scale follow the endpoint's declared coordinate_format (Gemini ER's
    # [y, x] stays the default); a batch that clearly arrived in 0-1 or raw
    # pixels is rescaled instead.
    coordinate_format = _endpoint_coordinate_format(llm)
    image_size = _image_dimensions(image_data)
    batch_scale = _detected_batch_scale(fused_results)
    for item in fused_results:
        if isinstance(item, dict):
            box_derived = _normalize_detected_box(item, coordinate_format, image_size, batch_scale)
            if not box_derived:
                _normalize_detected_point(item, coordinate_format, image_size, batch_scale)

    detected_labels = set(
        item["label"] for item in fused_results if isinstance(item, dict) and "label" in item
    )
    failed_queries = [query for query in queries if query not in detected_labels]

    result_dict = {"detected": fused_results, "failed": failed_queries}

    if failed_queries:
        result_dict["message"] = (
            "Detection failed for some targets. Please try changing to a more"
            " detailed description (e.g., describe its shape, color, or nearby"
            " text)."
        )

    logger.info("Fused results from parallel inference.")
    return result_dict


async def _create_error_command(ctx, state, tool_call_id, error_message, wrapper):
    return ToolMessage(
        tool_call_id=tool_call_id or "default_tool_call",
        content=wrapper.on_failure_fn(error_message),
        additional_kwargs={"error": error_message},
        status="error",
    )


async def _create_success_command(ctx, state, tool_call_id, output, wrapper):
    return ToolMessage(
        tool_call_id=tool_call_id or "default_tool_call",
        content=wrapper.on_success_fn(output),
        status="success",
    )
