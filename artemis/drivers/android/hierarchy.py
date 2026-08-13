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

"""Android XML Layout Hierarchy parser and semantic node extractor."""

import re
from typing import Any
import xml.etree.ElementTree as ET
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


def parse_ui_hierarchy(xml_string: str) -> list[dict[str, Any]]:
    """Parses raw Android XML hierarchy dump into structured interactive UI nodes."""
    if not xml_string:
        return []

    elements: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_string)
        for node in root.iter():
            attribs = node.attrib
            bounds_str = attribs.get("bounds", "")
            match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
            if not match:
                continue

            x1, y1, x2, y2 = map(int, match.groups())
            # Filter zero-area nodes
            if x1 >= x2 or y1 >= y2:
                continue

            text = attribs.get("text") or attribs.get("content-desc") or ""
            class_name = attribs.get("class", "")
            resource_id = attribs.get("resource-id", "")
            clickable = attribs.get("clickable") == "true"

            if text or clickable or resource_id:
                elements.append(
                    {
                        "text": text,
                        "resource_id": resource_id,
                        "class": class_name,
                        "clickable": clickable,
                        "bounds": [x1, y1, x2, y2],
                        "center": [(x1 + x2) // 2, (y1 + y2) // 2],
                    }
                )
    except Exception as e:
        logger.warning(f"Failed to parse XML hierarchy: {e}")

    return elements
