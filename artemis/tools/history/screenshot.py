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

"""get_step_screenshot: one recorded screenshot of one step, on demand.

``pre`` is the screen observed at the start of the step, ``post`` the screen
after its action (when recorded), ``overlay`` the pre screenshot with the
step's action drawn on it (tap point / swipe path) so a reviewer can check
that the action landed on the intended element. The overlay is drawn from the
step's *raw* stored action (physical pixels), never from the normalized twin.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any

from artemis.utils.logger import get_logger
from artemis.utils.visualization import overlay_action_on_screenshot

logger = get_logger(__name__)

WHICH_CHOICES = ("pre", "post", "overlay")


@dataclass
class ScreenshotResult:
    """One screenshot lookup: a caption plus the JPEG bytes when a file exists."""

    step_number: int
    which: str
    description: str
    image_bytes: bytes | None = None
    #: True when ``which == "overlay"`` and the action was actually drawn.
    overlay_drawn: bool = False

    def to_content_blocks(self) -> list[dict[str, Any]] | str:
        """The tool's return value: content blocks with the image, or the
        plain-text explanation when there is no image to attach."""
        if not self.image_bytes:
            return self.description
        encoded = base64.b64encode(self.image_bytes).decode("utf-8")
        return [
            {"type": "text", "text": self.description},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
        ]


def load_step_screenshot(reader: Any, step_number: Any, which: str = "pre") -> ScreenshotResult:
    """Loads (and for ``overlay`` annotates) one step screenshot from ``reader``."""
    which = str(which or "pre").lower()
    try:
        number = int(step_number)
    except (TypeError, ValueError):
        return ScreenshotResult(
            -1, which, f"Error: step_number must be an integer, got {step_number!r}."
        )
    if which not in WHICH_CHOICES:
        return ScreenshotResult(
            number, which, f"Error: 'which' must be 'pre', 'post' or 'overlay', got '{which}'."
        )
    if reader is None:
        return ScreenshotResult(number, which, "Error: no execution history available.")

    source = "pre" if which == "overlay" else which
    try:
        path = reader.get_step_image_path(number, source)
    except (sqlite3.Error, OSError, ValueError) as e:
        return ScreenshotResult(number, which, f"Error looking up step {number}: {e}")
    if path is None:
        try:
            record = reader.get_step_record(number)
        except (sqlite3.Error, ValueError):
            record = None
        if record is None:
            return ScreenshotResult(number, which, f"Error: step {number} not found.")
        return ScreenshotResult(
            number, which, f"No {source}-action screenshot recorded for step {number}."
        )
    try:
        image_bytes = Path(path).read_bytes()
    except OSError as e:
        return ScreenshotResult(number, which, f"Error reading screenshot: {e}")

    if which != "overlay":
        return ScreenshotResult(
            number, which, f"Screenshot of step {number} ({which}-action) is attached.", image_bytes
        )

    try:
        record = reader.get_step_record(number)
    except (sqlite3.Error, ValueError):
        record = None
    action = getattr(record, "action_taken", None) if record is not None else None
    try:
        annotated = overlay_action_on_screenshot(image_bytes, action)
    except (TypeError, ValueError, OSError) as e:
        logger.debug(f"Action overlay for step {number} failed: {e}")
        annotated = None
    if annotated is None:
        return ScreenshotResult(
            number,
            which,
            f"Screenshot of step {number} (pre-action; the step's action could not be"
            " drawn, plain screenshot) is attached.",
            image_bytes,
        )
    return ScreenshotResult(
        number,
        which,
        f"Screenshot of step {number} (pre-action with the step's action drawn on it) is attached.",
        annotated,
        overlay_drawn=True,
    )
