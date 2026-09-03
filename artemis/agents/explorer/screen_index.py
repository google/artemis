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

"""In-memory index of the current screen's UI elements for the Explorer.

Built from the fused XML/OCR hierarchy so searches do not depend on the Data
Engine having persisted the frame. Supports fuzzy text search, exact matches
and coordinate hit testing.
"""

from dataclasses import dataclass, field
import difflib
import re
from typing import Any, Literal

from artemis.utils.visualization import parse_bounds

Bounds = tuple[int, int, int, int]
ElementSource = Literal["xml", "ocr"]

#: Attributes that mark an XML node as something the user can interact with.
INTERACTION_KEYS = (
    "clickable",
    "scrollable",
    "long-clickable",
    "checkable",
    "focusable",
    "editable",
    "selected",
)

#: Minimum query length for substring matches to count as strong evidence;
#: shorter fragments ("ok", "a") match far too much.
_SUBSTRING_MIN_CHARS = 3
_SUBSTRING_SCORE = 0.85


def normalize_text(value: Any) -> str:
    """Case-folds and collapses whitespace so visually equal labels compare equal."""
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def text_similarity(query: str, text: str) -> float:
    """Similarity in ``[0, 1]`` between a query and an element label.

    Uses the Data Engine's ``SequenceMatcher`` ratio, with a minimum score
    for substring matches when the query is at least three characters long.
    """
    q, t = normalize_text(query), normalize_text(text)
    if not q or not t:
        return 0.0
    ratio = difflib.SequenceMatcher(None, q, t).ratio()
    if len(q) >= _SUBSTRING_MIN_CHARS and (q in t or t in q):
        ratio = max(ratio, _SUBSTRING_SCORE)
    return ratio


def _is_truthy_attr(value: Any) -> bool:
    return value is True or str(value).strip().lower() == "true"


@dataclass(frozen=True)
class ScreenElement:
    """One text-bearing element of the screen with pixel bounds."""

    text: str
    bounds: Bounds
    source: ElementSource
    class_name: str | None = None
    resource_id: str | None = None
    interactive: bool = False
    node: dict[str, Any] = field(default_factory=dict, compare=False, hash=False, repr=False)

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2

    @property
    def area(self) -> int:
        left, top, right, bottom = self.bounds
        return max(1, right - left) * max(1, bottom - top)

    def contains(self, x: int, y: int) -> bool:
        left, top, right, bottom = self.bounds
        return left <= x <= right and top <= y <= bottom

    def overlap_ratio(self, other: "ScreenElement") -> float:
        """Intersection over the smaller area; 1.0 when one box covers the other."""
        l1, t1, r1, b1 = self.bounds
        l2, t2, r2, b2 = other.bounds
        inter_w = min(r1, r2) - max(l1, l2)
        inter_h = min(b1, b2) - max(t1, t2)
        if inter_w <= 0 or inter_h <= 0:
            return 0.0
        return (inter_w * inter_h) / min(self.area, other.area)

    def describe(self) -> str:
        """Compact one-line description for tool text output."""
        parts = [f"'{self.text}'"] if self.text else []
        if self.class_name:
            parts.append(f"class={self.class_name}")
        if self.resource_id:
            parts.append(f"id={self.resource_id}")
        parts.append("source=ocr" if self.source == "ocr" else "source=ui-tree")
        if self.interactive:
            parts.append("interactive")
        return " ".join(parts)


@dataclass(frozen=True)
class TextMatch:
    element: ScreenElement
    score: float
    matched_text: str


class ScreenIndex:
    """Queryable snapshot of the visible, text-bearing elements of one screen."""

    def __init__(self, elements: list[ScreenElement], width: int, height: int):
        self.elements = elements
        self.width = width
        self.height = height

    def __len__(self) -> int:
        return len(self.elements)

    @property
    def ocr_elements(self) -> list[ScreenElement]:
        return [e for e in self.elements if e.source == "ocr"]

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    @classmethod
    def empty(cls, width: int, height: int) -> "ScreenIndex":
        return cls([], width, height)

    @classmethod
    def from_hierarchy(
        cls, fused_xml: list[dict[str, Any]] | None, width: int, height: int
    ) -> "ScreenIndex":
        """Builds the index from a fused UI hierarchy (XML nodes plus OCR text).

        Off-screen and zero-area boxes are dropped, and the same label at the
        same place is kept once.  Nodes without any label are skipped: the
        index exists to answer text and hit-test queries, and an unlabeled
        wrapper answers neither usefully.
        """
        elements: list[ScreenElement] = []
        seen: set[tuple[str, Bounds]] = set()

        def add(element: ScreenElement) -> None:
            key = (normalize_text(element.text), element.bounds)
            if key in seen or not cls._is_visible(element.bounds, width, height):
                return
            seen.add(key)
            elements.append(element)

        for node in fused_xml or []:
            if not isinstance(node, dict):
                continue
            class_name = node.get("class") or node.get("className")
            resource_id = node.get("resource-id") or node.get("resourceId")
            interactive = any(_is_truthy_attr(node.get(key)) for key in INTERACTION_KEYS)

            for ocr in node.get("ocr_elements") or []:
                ocr_bounds = parse_bounds(ocr.get("bounds")) if isinstance(ocr, dict) else None
                ocr_text = str(ocr.get("text") or "").strip() if isinstance(ocr, dict) else ""
                if ocr_bounds and ocr_text:
                    add(
                        ScreenElement(
                            text=ocr_text,
                            bounds=ocr_bounds,
                            source="ocr",
                            class_name=str(class_name) if class_name else None,
                            resource_id=str(resource_id) if resource_id else None,
                            interactive=interactive,
                            node=node,
                        )
                    )

            label = str(node.get("text") or node.get("content-desc") or "").strip()
            bounds = parse_bounds(node.get("bounds"))
            if label and bounds:
                add(
                    ScreenElement(
                        text=label,
                        bounds=bounds,
                        source="xml",
                        class_name=str(class_name) if class_name else None,
                        resource_id=str(resource_id) if resource_id else None,
                        interactive=interactive,
                        node=node,
                    )
                )
        return cls(elements, width, height)

    @staticmethod
    def _is_visible(bounds: Bounds, width: int, height: int) -> bool:
        left, top, right, bottom = bounds
        if right <= left or bottom <= top:
            return False
        return right > 0 and bottom > 0 and left < width and top < height

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #

    def search_text(self, query: str, threshold: float = 0.6, limit: int = 8) -> list[TextMatch]:
        """Fuzzy label search, best matches first."""
        matches: list[TextMatch] = []
        for element in self.elements:
            score = text_similarity(query, element.text)
            if score > threshold:
                matches.append(TextMatch(element=element, score=score, matched_text=element.text))
        matches.sort(key=lambda m: (-m.score, m.element.area))
        return matches[:limit]

    def exact_matches(self, query: str) -> list[ScreenElement]:
        """Elements whose label equals ``query`` after normalization.

        Overlapping duplicates (the same label from the UI tree and from OCR
        at the same place) collapse to one element, preferring the UI-tree
        node because it carries structure the OCR box lacks.
        """
        wanted = normalize_text(query)
        if not wanted:
            return []
        hits = [e for e in self.elements if normalize_text(e.text) == wanted]
        hits.sort(key=lambda e: (e.source != "xml", e.area))
        unique: list[ScreenElement] = []
        for element in hits:
            if all(element.overlap_ratio(kept) < 0.5 for kept in unique):
                unique.append(element)
        return unique

    def elements_at(self, x: int, y: int) -> list[ScreenElement]:
        """Elements covering pixel ``(x, y)``: UI-tree nodes innermost first, then OCR.

        Mirrors the historical coordinate audit: UI-tree candidates stop at the
        first interactive node (at most two are reported) so the answer names
        what a tap would actually hit rather than every enclosing wrapper.
        """
        covering = [e for e in self.elements if e.contains(x, y)]
        xml = sorted((e for e in covering if e.source == "xml"), key=lambda e: e.area)
        picked: list[ScreenElement] = []
        for element in xml:
            picked.append(element)
            if element.interactive or len(picked) >= 2:
                break
        ocr = sorted((e for e in covering if e.source == "ocr"), key=lambda e: e.area)
        return picked + ocr
