"""Reads a slider's position from its pixels, in the unit a person would use.

The accessibility tree gives a SeekBar's bounds and nothing else: no value, no
range. A model that can only see the bounds has to guess where the thumb sits,
and on 2026-11-11 that guess turned "set the brightness to about 70%" into 21
touches. The fill under those bounds carries the value, so this reads it.
"""

from __future__ import annotations

import re

from PIL import Image

_MIN_CHANNEL_SPREAD = 60
_COLOR_TOLERANCE = 40
_SCANLINE_FRACTIONS = (4, 8, 12)


def _is_same_color(pixel: tuple[int, int, int], reference: tuple[int, int, int]) -> bool:
    return all(abs(pixel[channel] - reference[channel]) < _COLOR_TOLERANCE for channel in range(3))


def measure_fill_percent(
    image: Image.Image,
    bounds: tuple[int, int, int, int],
) -> int | None:
    """Returns the filled share of a left-origin track as 0-100, or None.

    The fill carries glyphs: the brightness bar draws a sun icon 8 pixels in, and
    a run-length read stops there and reports 1%. The value is the last filled
    pixel, and a track whose left end is not filled has no left-origin value to
    report, so this returns None rather than a guess.
    """
    left, top, right, bottom = bounds
    width = right - left
    height = bottom - top
    if width <= 1 or height <= 1:
        return None
    if image.mode != "RGB":
        image = image.convert("RGB")

    samples: list[int] = []
    for fraction in _SCANLINE_FRACTIONS:
        y = top + height * fraction // 16
        row = [image.getpixel((x, y)) for x in range(left, right)]
        filled = [pixel for pixel in row if max(pixel) - min(pixel) >= _MIN_CHANNEL_SPREAD]
        if not filled:
            continue
        reference = tuple(
            sorted(pixel[channel] for pixel in filled)[len(filled) // 2] for channel in range(3)
        )
        matching = [index for index, pixel in enumerate(row) if _is_same_color(pixel, reference)]
        if not matching or matching[0] > width // 20:
            continue
        samples.append(round((matching[-1] + 1) * 100 / width))

    if not samples:
        return None
    samples.sort()
    return samples[len(samples) // 2]


ADJUSTABLE_CLASSES = ("SeekBar", "ProgressBar", "Slider", "RatingBar")


def _parse_bounds(text: object) -> tuple[int, int, int, int] | None:
    if not isinstance(text, str):
        return None
    digits = text.replace("[", " ").replace("]", " ").replace(",", " ").split()
    if len(digits) != 4:
        return None
    try:
        left, top, right, bottom = (int(value) for value in digits)
    except ValueError:
        return None
    return left, top, right, bottom


def measure_control_values(image: Image.Image, nodes: list[dict]) -> dict[str, int]:
    """Returns ``{bounds string: percent}`` for every adjustable control it can read.

    Keyed by the node's own bounds string so the caller can match it back without
    a second traversal, and only controls whose fill it actually measured appear.
    """
    values: dict[str, int] = {}
    for node in nodes:
        short_class = str(node.get("class") or "").rsplit(".", 1)[-1]
        if short_class not in ADJUSTABLE_CLASSES:
            continue
        bounds = _parse_bounds(node.get("bounds"))
        if bounds is None:
            continue
        percent = measure_fill_percent(image, bounds)
        if percent is not None:
            values[str(node.get("bounds"))] = percent
    return values


def point_at_share(
    bounds: tuple[int, int, int, int], share: int, width: int, height: int
) -> list[int]:
    """Returns the normalized [x, y] a finger would aim at for ``share`` percent of the track."""
    left, top, right, bottom = bounds
    clamped = max(0, min(100, share))
    x = left + (right - left) * clamped // 100
    y = (top + bottom) // 2
    return [round(x * 1000 / width), round(y * 1000 / height)]


def snap_press_point(
    start: list[int],
    controls: list[tuple[tuple[int, int, int, int], int]],
    width: int,
    height: int,
) -> list[int]:
    """Moves a drag's press point onto the thumb of the control it lands on.

    The release point is the caller's intent and stays untouched; only where the
    finger comes down is corrected, because a model aims at the thumb and reads
    its position off the screenshot about ten points wrong.
    """
    if len(start) != 2:
        return start
    px = round(start[0] * width / 1000)
    py = round(start[1] * height / 1000)
    for (left, top, right, bottom), percent in controls:
        if not (left <= px <= right and top <= py <= bottom):
            continue
        thumb = left + (right - left) * percent // 100
        return [round(thumb * 1000 / width), start[1]]
    return start


def share_from_text(text: str) -> int | None:
    """Returns the percentage a goal names ("把亮度调到约70%"), or None."""
    match = re.search(r"(\d{1,3})\s*%", text or "")
    if match is None:
        return None
    return max(0, min(100, int(match.group(1))))
