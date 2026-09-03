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

"""Screen geometry shared by the Explorer agent and the ``ask_explorer`` tool.

Explorer candidates use normalized ``[x, y]`` coordinates on a 0-1000 scale;
device actions use pixels.
"""

from typing import Any

#: Fallback screen size when neither the operator observation nor the device
#: context reports one (a common phone portrait resolution).
FALLBACK_SCREEN_SIZE: tuple[int, int] = (1080, 2400)

NORMALIZED_MAX = 1000


def resolve_screen_size(ctx: Any, state: Any) -> tuple[int, int]:
    """Returns ``(width, height)`` in pixels for the current screenshot.

    Precedence: the operator's latest raw observation (which measured the
    screenshot it annotated), then the device context, then the fallback.
    """
    raw = getattr(state, "operator_raw_data", None) or {}
    width = raw.get("width") if isinstance(raw, dict) else None
    height = raw.get("height") if isinstance(raw, dict) else None
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        return width, height

    device = getattr(ctx, "device", None)
    dev_w = getattr(device, "device_width", None) if device else None
    dev_h = getattr(device, "device_height", None) if device else None
    fb_w, fb_h = FALLBACK_SCREEN_SIZE
    width = dev_w if isinstance(dev_w, int) and dev_w > 0 else fb_w
    height = dev_h if isinstance(dev_h, int) and dev_h > 0 else fb_h
    return width, height


def norm_to_pixel(nx: float, ny: float, width: int, height: int) -> tuple[int, int]:
    """Maps normalized 0-1000 coordinates to clamped pixel coordinates."""
    px = int(max(0, min(width, nx * width / NORMALIZED_MAX)))
    py = int(max(0, min(height, ny * height / NORMALIZED_MAX)))
    return px, py


def pixel_to_norm(px: float, py: float, width: int, height: int) -> tuple[int, int]:
    """Maps pixel coordinates to clamped normalized 0-1000 coordinates."""
    nx = int(max(0, min(NORMALIZED_MAX, px * NORMALIZED_MAX / width)))
    ny = int(max(0, min(NORMALIZED_MAX, py * NORMALIZED_MAX / height)))
    return nx, ny


def is_valid_norm_point(coords: Any) -> bool:
    """True when ``coords`` is a 2-item ``[nx, ny]`` inside the 0-1000 range."""
    if not isinstance(coords, (list, tuple)) or len(coords) != 2:
        return False
    try:
        nx, ny = int(coords[0]), int(coords[1])
    except (TypeError, ValueError):
        return False
    return 0 <= nx <= NORMALIZED_MAX and 0 <= ny <= NORMALIZED_MAX
