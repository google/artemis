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

import io
import os
import re
from typing import Any

from artemis.utils.coordinates import parse_swipe_parameters
from PIL import Image, ImageDraw, ImageFont


def draw_bounding_boxes(
    image_path: str, boxes: list[list[int]], labels: list[str], output_path: str
):
    """Draw bounding boxes and labels on an image.

    Args:
        image_path: Path to the original image.
        boxes: List of boxes, each box is [left, top, right, bottom].
        labels: List of labels corresponding to each box.
        output_path: Path to save the annotated image.
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image not found at {image_path}")

    img = Image.open(image_path)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    draw = ImageDraw.Draw(img)

    # Use a default font or try to load a system font
    try:
        font = ImageFont.load_default(size=45)
    except Exception:
        font = None

    for box, label in zip(boxes, labels):
        left, top, right, bottom = box

        # Draw rectangle
        draw.rectangle([left, top, right, bottom], outline="red", width=3)

        # Draw label with outline stroke for high contrast
        if font:
            draw.text(
                (left, top),
                label,
                fill="red",
                font=font,
                stroke_width=5,
                stroke_fill="white",
            )
        else:
            draw.text(
                (left, top),
                label,
                fill="red",
                stroke_width=5,
                stroke_fill="white",
            )
    img.save(output_path)


def draw_dots(
    image_path: str,
    points: list[list[int]],
    labels: list[str],
    output_path: str,
    radius: int = 10,
    color: str = "red",
):
    """Draw dots and labels on an image.

    Args:
        image_path: Path to the original image.
        points: List of points, each point is [x, y].
        labels: List of labels corresponding to each point.
        output_path: Path to save the annotated image.
        radius: Radius of the dot.
        color: Color of the indicator dot and text label.
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image not found at {image_path}")

    img = Image.open(image_path)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.load_default(size=45)
    except Exception:
        font = None

    # 1. Draw all background and foreground dots first
    for point in points:
        x, y = point

        # Draw larger white background dot (2x radius)
        white_radius = 2 * radius
        draw.ellipse(
            [
                x - white_radius,
                y - white_radius,
                x + white_radius,
                y + white_radius,
            ],
            fill="white",
            outline="white",
        )

        # Draw colored indicator dot on top
        draw.ellipse(
            [x - radius, y - radius, x + radius, y + radius],
            fill=color,
            outline=color,
        )

    # 2. Draw all text labels on top
    for point, label in zip(points, labels):
        x, y = point
        white_radius = 2 * radius

        # Draw label shifted to prevent overlap with white dot and outline with a white stroke
        if font:
            draw.text(
                (x + white_radius + 4, y - 22),
                label,
                fill=color,
                font=font,
                stroke_width=5,
                stroke_fill="white",
            )
        else:
            draw.text(
                (x + white_radius + 4, y - 22),
                label,
                fill=color,
                stroke_width=5,
                stroke_fill="white",
            )

    img.save(output_path)


def parse_bounds(bounds_str: str | None) -> tuple[int, int, int, int] | None:
    """Parses standard Android '[left,top][right,bottom]' bounds string."""
    if not bounds_str:
        return None
    match = re.match(r"\[(\-?\d+),(\-?\d+)\]\[(\-?\d+),(\-?\d+)\]", bounds_str)
    if match:
        return tuple(map(int, match.groups()))
    return None


def get_center_coordinates(left: int, top: int, right: int, bottom: int) -> tuple[int, int]:
    """Returns pixel center coordinates of a bounding box."""
    return (left + right) // 2, (top + bottom) // 2


def _inject_mutual_occlusion_warnings(
    lines: list[str], items_bounds: list[tuple[int, tuple[int, int, int, int]]]
) -> list[str]:
    """Detect pairs of elements with >= 50% overlap and append a soft, restrained warning

    to the generated element description lines.
    Excludes pure parent-child enclosing bounds to prevent noise on standard
    nested containers.
    """
    if len(items_bounds) < 2:
        return lines

    warnings_map: dict[int, list[int]] = {}
    n = len(items_bounds)

    for i in range(n):
        idx_i, b_i = items_bounds[i]
        l_i, t_i, r_i, bot_i = b_i
        area_i = max(1, (r_i - l_i) * (bot_i - t_i))

        for j in range(i + 1, n):
            idx_j, b_j = items_bounds[j]
            l_j, t_j, r_j, bot_j = b_j
            area_j = max(1, (r_j - l_j) * (bot_j - t_j))

            # Calculate intersection
            inter_l = max(l_i, l_j)
            inter_t = max(t_i, t_j)
            inter_r = min(r_i, r_j)
            inter_bot = min(bot_i, bot_j)

            if inter_l >= inter_r or inter_t >= inter_bot:
                continue

            inter_area = (inter_r - inter_l) * (inter_bot - inter_t)
            ratio_i = inter_area / area_i
            ratio_j = inter_area / area_j

            # To be considered typical parent-child nesting (and NOT occlusion), one node must be inside another,
            # AND their centers must be closely aligned (i.e. concentric/enclosing layout).
            # If one node overlaps/sits inside another but their centers are significantly offset (e.g. a right-aligned Floating Action Button covering the right end of a left-aligned list item), it is mutual occlusion!
            center_i_x = (l_i + r_i) / 2.0
            center_i_y = (t_i + bot_i) / 2.0
            center_j_x = (l_j + r_j) / 2.0
            center_j_y = (t_j + bot_j) / 2.0
            center_dist = ((center_i_x - center_j_x) ** 2 + (center_i_y - center_j_y) ** 2) ** 0.5

            max_dim = max(r_i - l_i, r_j - l_j, bot_i - t_i, bot_j - t_j)
            is_concentric = center_dist < (max_dim * 0.20)

            is_i_inside_j = (
                (l_i >= l_j and t_i >= t_j and r_i <= r_j and bot_i <= bot_j)
                and (area_j > area_i * 2.0)
                and is_concentric
            )
            is_j_inside_i = (
                (l_j >= l_i and t_j >= t_i and r_j <= r_i and bot_j <= bot_i)
                and (area_i > area_j * 2.0)
                and is_concentric
            )
            if is_i_inside_j or is_j_inside_i:
                continue

            if ratio_i >= 0.50 or ratio_j >= 0.50:
                warnings_map.setdefault(idx_i, []).append(idx_j)
                warnings_map.setdefault(idx_j, []).append(idx_i)

    if not warnings_map:
        return lines

    new_lines = []
    for line in lines:
        match = re.match(r"^\[(\d+)\]", line)
        if match:
            el_idx = int(match.group(1))
            if el_idx in warnings_map:
                overlaps = sorted(list(set(warnings_map[el_idx])))
                overlaps_str = " and ".join(f"[{o}]" for o in overlaps[:2])
                if len(overlaps) > 2:
                    overlaps_str += f" (+{len(overlaps) - 2} more)"
                line = f"{line} (WARNING: may overlap with {overlaps_str}, possible occlusion)"
        new_lines.append(line)
    return new_lines


def format_minimal_list_with_points(
    fused_xml: list[dict[str, Any]], width: int = 1080, height: int = 2400
) -> tuple[str, list[list[int]], list[str]]:
    """Formats fused XML hierarchy into a minimal list with sequence numbers,

    and returns physical coordinate points & labels for drawing.
    """
    lines = []
    points = []
    labels = []
    idx = 1
    added_items = []  # list of tuples: (text, cx, cy)
    items_bounds = []  # list of tuples: (idx, (left, top, right, bottom))

    def normalize_bounds(left: int, top: int, right: int, bottom: int) -> str:
        n_left = int(max(0, min(1000, left * 1000 / width)))
        n_top = int(max(0, min(1000, top * 1000 / height)))
        n_right = int(max(0, min(1000, right * 1000 / width)))
        n_bottom = int(max(0, min(1000, bottom * 1000 / height)))
        return f"[{n_left},{n_top}][{n_right},{n_bottom}]"

    def is_duplicate(text_val: str, cx: int, cy: int) -> bool:
        text_clean = text_val.strip()
        for ex_text, ex_cx, ex_cy in added_items:
            if ex_text == text_clean:
                dist = ((cx - ex_cx) ** 2 + (cy - ex_cy) ** 2) ** 0.5
                if dist < 8.0:
                    return True
        return False

    for node in fused_xml:
        text = node.get("text") or node.get("content-desc") or ""
        bounds_str = node.get("bounds")
        bounds = parse_bounds(bounds_str)

        ocr_elements = node.get("ocr_elements")
        registered = False

        if ocr_elements:
            for ocr in ocr_elements:
                ocr_bounds = parse_bounds(ocr.get("bounds"))
                if ocr_bounds:
                    left, top, right, bottom = ocr_bounds
                    cx, cy = get_center_coordinates(left, top, right, bottom)
                    if is_duplicate(ocr["text"], cx, cy):
                        continue

                    norm_bounds = normalize_bounds(left, top, right, bottom)
                    lines.append(f"[{idx}] OCR Text: '{ocr['text']}' | Bounds: {norm_bounds}")
                    items_bounds.append((idx, (left, top, right, bottom)))

                    points.append([cx, cy])
                    labels.append(str(idx))
                    added_items.append((ocr["text"].strip(), cx, cy))
                    idx += 1
                    registered = True

        if not registered and text.strip() and bounds:
            left, top, right, bottom = bounds
            cx, cy = get_center_coordinates(left, top, right, bottom)
            if is_duplicate(text, cx, cy):
                continue

            norm_bounds = normalize_bounds(left, top, right, bottom)
            lines.append(f"[{idx}] Text: '{text.strip()}' | Bounds: {norm_bounds}")
            items_bounds.append((idx, (left, top, right, bottom)))

            points.append([cx, cy])
            labels.append(str(idx))
            added_items.append((text.strip(), cx, cy))
            idx += 1

    lines = _inject_mutual_occlusion_warnings(lines, items_bounds)
    return "\n".join(lines), points, labels


def format_minimal_list_with_elements(
    fused_xml: list[dict[str, Any]], width: int = 1080, height: int = 2400
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Formats fused XML hierarchy into a minimal list with sequence numbers,

    and returns structured element metadata & labels for drawing/validation.
    """
    lines = []
    elements = []
    labels = []
    idx = 1
    added_items = []  # list of tuples: (text, cx, cy)
    items_bounds = []  # list of tuples: (idx, (left, top, right, bottom))

    def normalize_bounds(left: int, top: int, right: int, bottom: int) -> str:
        n_left = int(max(0, min(1000, left * 1000 / width)))
        n_top = int(max(0, min(1000, top * 1000 / height)))
        n_right = int(max(0, min(1000, right * 1000 / width)))
        n_bottom = int(max(0, min(1000, bottom * 1000 / height)))
        return f"[{n_left},{n_top}][{n_right},{n_bottom}]"

    def is_duplicate(text_val: str, cx: int, cy: int) -> bool:
        text_clean = text_val.strip()
        for ex_text, ex_cx, ex_cy in added_items:
            if ex_text == text_clean:
                dist = ((cx - ex_cx) ** 2 + (cy - ex_cy) ** 2) ** 0.5
                if dist < 8.0:
                    return True
        return False

    for node in fused_xml:
        text = node.get("text") or node.get("content-desc") or ""
        bounds_str = node.get("bounds")
        bounds = parse_bounds(bounds_str)

        ocr_elements = node.get("ocr_elements")
        registered = False

        if ocr_elements:
            for ocr in ocr_elements:
                ocr_bounds = parse_bounds(ocr.get("bounds"))
                if ocr_bounds:
                    left, top, right, bottom = ocr_bounds
                    cx, cy = get_center_coordinates(left, top, right, bottom)
                    if is_duplicate(ocr["text"], cx, cy):
                        continue

                    norm_bounds = normalize_bounds(left, top, right, bottom)
                    lines.append(f"[{idx}] OCR Text: '{ocr['text']}' | Bounds: {norm_bounds}")
                    items_bounds.append((idx, (left, top, right, bottom)))

                    elements.append(
                        {
                            "index": idx,
                            "center": [cx, cy],
                            "text": ocr["text"].strip(),
                            "bounds": [left, top, right, bottom],
                            "class": node.get("class"),
                            "resource_id": node.get("resource-id"),
                            "is_ocr": True,
                        }
                    )
                    labels.append(str(idx))
                    added_items.append((ocr["text"].strip(), cx, cy))
                    idx += 1
                    registered = True

        if not registered and text.strip() and bounds:
            left, top, right, bottom = bounds
            cx, cy = get_center_coordinates(left, top, right, bottom)
            if is_duplicate(text, cx, cy):
                continue

            norm_bounds = normalize_bounds(left, top, right, bottom)
            lines.append(f"[{idx}] Text: '{text.strip()}' | Bounds: {norm_bounds}")
            items_bounds.append((idx, (left, top, right, bottom)))

            elements.append(
                {
                    "index": idx,
                    "center": [cx, cy],
                    "text": text.strip(),
                    "bounds": [left, top, right, bottom],
                    "class": node.get("class"),
                    "resource_id": node.get("resource-id"),
                    "is_ocr": False,
                }
            )
            labels.append(str(idx))
            added_items.append((text.strip(), cx, cy))
            idx += 1

    lines = _inject_mutual_occlusion_warnings(lines, items_bounds)
    return "\n".join(lines), elements, labels


def crop_and_annotate_target(
    image_bytes: bytes,
    coords: list[int],
    crop_size: int | None = 256,
    dot_radius: int = 8,
) -> bytes:
    """Crops a square region centered around coords, and draws a red dot with black outline at coords.

    If crop_size is None, returns the full annotated image.
    """
    img = Image.open(io.BytesIO(image_bytes))
    width, height = img.size
    x, y = coords

    # Ensure coordinates are within image boundaries
    x = max(0, min(x, width - 1))
    y = max(0, min(y, height - 1))

    if crop_size is not None:
        # Calculate crop boundaries (centered around x, y; clamped to image boundaries)
        w_crop = min(crop_size, width)
        h_crop = min(crop_size, height)

        x_min = max(0, min(x - w_crop // 2, width - w_crop))
        y_min = max(0, min(y - h_crop // 2, height - h_crop))
        x_max = x_min + w_crop
        y_max = y_min + h_crop

        # Crop
        cropped_img = img.crop((x_min, y_min, x_max, y_max))

        # Local coordinates within the cropped frame
        local_x = x - x_min
        local_y = y - y_min
    else:
        cropped_img = img
        local_x = x
        local_y = y

    # Draw solid red circle with a black outline
    draw = ImageDraw.Draw(cropped_img)
    draw.ellipse(
        [
            local_x - dot_radius,
            local_y - dot_radius,
            local_x + dot_radius,
            local_y + dot_radius,
        ],
        fill="red",
        outline="black",
        width=2 if crop_size is None else 1,
    )

    out_buf = io.BytesIO()
    if crop_size is None:
        cropped_img.save(out_buf, format="JPEG", quality=85)
    else:
        cropped_img.save(out_buf, format="PNG")
    return out_buf.getvalue()


def _resolve_coordinates(coord: Any, width: int, height: int) -> tuple[int, int] | None:
    """Helper to safely parse coordinates into pixel space."""
    if not isinstance(coord, (list, tuple)) or len(coord) < 2:
        return None
    try:
        x, y = float(coord[0]), float(coord[1])
        # If coordinates are normalized (0.0 <= x <= 1.0)
        if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
            px = int(round(x * width))
            py = int(round(y * height))
        elif 0 <= x <= 1000 and 0 <= y <= 1000 and (width > 1000 or height > 1000):
            # Android normalized 1000 coordinate space
            px = int(round(x * width / 1000.0))
            py = int(round(y * height / 1000.0))
        else:
            px = int(round(x))
            py = int(round(y))
        px = max(0, min(width - 1, px))
        py = max(0, min(height - 1, py))
        return px, py
    except Exception:
        return None


def draw_action_overlay_on_image(
    image_bytes: bytes,
    action_name: str,
    action_args: dict[str, Any],
) -> bytes:
    """Draws visual action indicators (tap ripples, sequence numbers, swipe arrows, focus boxes)

    on top of the before-action screenshot.

    Args:
        image_bytes: Raw JPEG/PNG image bytes of the screenshot.
        action_name: Action name (e.g. 'click', 'click_sequence', 'swipe', 'input_text').
        action_args: Action parameters dictionary.

    Returns:
        Annotated JPEG image bytes, or original image_bytes on error.
    """
    import math

    if not image_bytes:
        return image_bytes

    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        width, height = img.size
        draw = ImageDraw.Draw(img)

        # Scale indicator sizes dynamically based on screen resolution
        base_dim = min(width, height)
        dot_radius = max(18, int(base_dim * 0.025))
        line_width = max(4, int(base_dim * 0.006))

        action = (action_name or "").lower()

        # 1. Single Click / Tap
        if action in ("click", "tap", "click_coordinate", "long_click", "press"):
            coord = (
                action_args.get("target")
                or action_args.get("coordinates")
                or action_args.get("point")
            )
            pt = _resolve_coordinates(coord, width, height)
            if pt:
                x, y = pt
                # Outer white aura
                draw.ellipse(
                    [
                        x - dot_radius * 1.5,
                        y - dot_radius * 1.5,
                        x + dot_radius * 1.5,
                        y + dot_radius * 1.5,
                    ],
                    outline="white",
                    width=line_width + 2,
                )
                # Red ripple circle
                draw.ellipse(
                    [x - dot_radius, y - dot_radius, x + dot_radius, y + dot_radius],
                    fill="red",
                    outline="white",
                    width=line_width,
                )
                # Crosshair
                cross_len = int(dot_radius * 1.8)
                draw.line([(x - cross_len, y), (x + cross_len, y)], fill="white", width=line_width)
                draw.line([(x, y - cross_len), (x, y + cross_len)], fill="white", width=line_width)

        # 2. Click Sequence
        elif action in ("click_sequence", "tap_sequence"):
            sequence = (
                action_args.get("sequence")
                or action_args.get("targets")
                or action_args.get("coordinates")
                or []
            )
            if isinstance(sequence, str):
                sequence_str = sequence.strip()
                try:
                    import json

                    sequence = json.loads(sequence_str)
                except Exception:
                    try:
                        import ast

                        sequence = ast.literal_eval(sequence_str)
                    except Exception:
                        pass

            if isinstance(sequence, list):
                pts = []
                for coord in sequence:
                    pt = _resolve_coordinates(coord, width, height)
                    if pt:
                        pts.append(pt)

                # Draw connecting lines/arrows between consecutive sequence points
                for i in range(len(pts) - 1):
                    p1, p2 = pts[i], pts[i + 1]
                    draw.line([p1, p2], fill="white", width=line_width + 3)
                    draw.line([p1, p2], fill="red", width=line_width)

                    # Arrow head
                    dx = p2[0] - p1[0]
                    dy = p2[1] - p1[1]
                    angle = math.atan2(dy, dx)
                    arrow_len = max(16, int(base_dim * 0.025))
                    arrow_angle = math.pi / 6
                    x1 = p2[0] - arrow_len * math.cos(angle - arrow_angle)
                    y1 = p2[1] - arrow_len * math.sin(angle - arrow_angle)
                    x2 = p2[0] - arrow_len * math.cos(angle + arrow_angle)
                    y2 = p2[1] - arrow_len * math.sin(angle + arrow_angle)
                    draw.polygon([p2, (x1, y1), (x2, y2)], fill="red", outline="white")

                # Draw numbered circular touch points
                for idx, (x, y) in enumerate(pts, 1):
                    # Outer aura
                    draw.ellipse(
                        [
                            x - dot_radius * 1.3,
                            y - dot_radius * 1.3,
                            x + dot_radius * 1.3,
                            y + dot_radius * 1.3,
                        ],
                        outline="white",
                        width=line_width + 1,
                    )
                    # Colored circle
                    draw.ellipse(
                        [x - dot_radius, y - dot_radius, x + dot_radius, y + dot_radius],
                        fill="red",
                        outline="white",
                        width=line_width,
                    )
                    label = str(idx)
                    try:
                        font = ImageFont.load_default(size=int(dot_radius * 1.2))
                        draw.text(
                            (x - dot_radius // 3, y - dot_radius // 2),
                            label,
                            fill="white",
                            font=font,
                            stroke_width=2,
                            stroke_fill="black",
                        )
                    except Exception:
                        draw.text(
                            (x - 4, y - 6),
                            label,
                            fill="white",
                        )

        # 3. Swipe / Drag / Scroll
        elif action in ("swipe", "drag", "scroll"):
            kind, target, _ = parse_swipe_parameters(action_args)
            start_pt, end_pt = None, None

            if kind == "coords" and isinstance(target, list) and len(target) == 4:
                start_pt = _resolve_coordinates(target[:2], width, height)
                end_pt = _resolve_coordinates(target[2:], width, height)
            elif kind == "direction" and isinstance(target, str):
                direction = target.lower()
                cx, cy = width // 2, height // 2
                dist_y = int(height * 0.35)
                dist_x = int(width * 0.35)
                if "up" in direction:
                    start_pt = (cx, cy + dist_y // 2)
                    end_pt = (cx, cy - dist_y // 2)
                elif "down" in direction:
                    start_pt = (cx, cy - dist_y // 2)
                    end_pt = (cx, cy + dist_y // 2)
                elif "left" in direction:
                    start_pt = (cx + dist_x // 2, cy)
                    end_pt = (cx - dist_x // 2, cy)
                elif "right" in direction:
                    start_pt = (cx - dist_x // 2, cy)
                    end_pt = (cx + dist_x // 2, cy)

            if start_pt and end_pt:
                sx, sy = start_pt
                ex, ey = end_pt

                draw.ellipse(
                    [
                        sx - dot_radius // 2,
                        sy - dot_radius // 2,
                        sx + dot_radius // 2,
                        sy + dot_radius // 2,
                    ],
                    fill="yellow",
                    outline="black",
                    width=2,
                )
                draw.line([(sx, sy), (ex, ey)], fill="white", width=line_width + 4)
                draw.line([(sx, sy), (ex, ey)], fill="red", width=line_width)

                dx = ex - sx
                dy = ey - sy
                angle = math.atan2(dy, dx)
                arrow_len = max(24, int(base_dim * 0.04))
                arrow_angle = math.pi / 6

                x1 = ex - arrow_len * math.cos(angle - arrow_angle)
                y1 = ey - arrow_len * math.sin(angle - arrow_angle)
                x2 = ex - arrow_len * math.cos(angle + arrow_angle)
                y2 = ey - arrow_len * math.sin(angle + arrow_angle)

                draw.polygon([(ex, ey), (x1, y1), (x2, y2)], fill="red", outline="white")

        # 4. Input Text
        elif action in ("input_text", "type"):
            coord = action_args.get("target") or action_args.get("coordinates")
            if coord:
                pt = _resolve_coordinates(coord, width, height)
                if pt:
                    x, y = pt
                    box_w = max(60, int(base_dim * 0.15))
                    box_h = max(30, int(base_dim * 0.06))
                    draw.rectangle(
                        [x - box_w, y - box_h, x + box_w, y + box_h],
                        outline="cyan",
                        width=line_width,
                    )

        out_buf = io.BytesIO()
        img.save(out_buf, format="JPEG", quality=85)
        return out_buf.getvalue()

    except Exception:
        return image_bytes
