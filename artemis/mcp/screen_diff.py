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

import re
from collections import Counter
from dataclasses import dataclass
from dataclasses import field

UI_DIFF_MARKER = "--- UI Changes Since Last Action ---"

NO_SCREEN_CHANGES = "(no UI changes since last action)"

_PRUNE_AFTER_MISSED_FRAMES = 200

_MAX_TRACKED_KEYS = 4096

_INDEX_PREFIX = re.compile(r"^\[\d+\]\s*")

ScreenElement = dict[str, object]


def _short_class(element: ScreenElement) -> str:
    return str(element.get("class") or "").rsplit(".", 1)[-1]


def _short_resource_id(element: ScreenElement) -> str:
    return str(element.get("resource_id") or "").rsplit("/", 1)[-1]


def _shown_text(element: ScreenElement) -> str:
    return str(element.get("text") or "")


def _is_ocr(element: ScreenElement) -> bool:
    return element.get("is_ocr") is True


def _strip_index(line: str) -> str:
    return _INDEX_PREFIX.sub("", line)


@dataclass
class DiffResult:
    lines: list[str] = field(default_factory=list)
    is_full: bool = False


@dataclass
class ScreenFrame:
    elements: list[ScreenElement] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    refs: list[int] = field(default_factory=list)
    width: int = 0
    height: int = 0


class ScreenRefAssigner:
    def __init__(self) -> None:
        self._next_ref = 1
        self._generation = 0
        self._seen_generation: dict[str, int] = {}
        self._ref_by_key: dict[str, int] = {}

    def assign(self, elements: list[ScreenElement]) -> list[int]:
        self._generation += 1
        occurrences: Counter[str] = Counter()
        refs: list[int] = []
        for element in elements:
            if _is_ocr(element):
                refs.append(0)
                continue
            base = "\x00".join(
                [_short_class(element), _shown_text(element), _short_resource_id(element)]
            )
            key = base + "\x00" + str(occurrences[base])
            occurrences[base] += 1
            ref = self._ref_by_key.get(key)
            if ref is None:
                ref = self._next_ref
                self._next_ref += 1
                self._ref_by_key[key] = ref
            self._seen_generation[key] = self._generation
            refs.append(ref)
        if len(self._ref_by_key) > _MAX_TRACKED_KEYS:
            stale = [
                key
                for key, seen in self._seen_generation.items()
                if self._generation - seen > _PRUNE_AFTER_MISSED_FRAMES
            ]
            for key in stale:
                del self._ref_by_key[key]
                del self._seen_generation[key]
        return refs


def _diff_frames(previous: ScreenFrame, current: ScreenFrame) -> DiffResult:
    if previous.width != current.width or previous.height != current.height:
        return DiffResult(is_full=True)
    previous_by_ref: dict[int, str] = {}
    previous_ocr: Counter[str] = Counter()
    for element, line, ref in zip(previous.elements, previous.lines, previous.refs):
        if _is_ocr(element):
            previous_ocr[_strip_index(line)] += 1
        elif ref > 0:
            previous_by_ref[ref] = _strip_index(line)
    lines: list[str] = []
    seen: set[int] = set()
    current_ocr: Counter[str] = Counter()
    for element, line, ref in zip(current.elements, current.lines, current.refs):
        if _is_ocr(element):
            stripped = _strip_index(line)
            current_ocr[stripped] += 1
            if current_ocr[stripped] > previous_ocr[stripped]:
                lines.append("+ " + line)
            continue
        seen.add(ref)
        old = previous_by_ref.get(ref)
        if old is None:
            lines.append("+ " + line)
        elif old != _strip_index(line):
            lines.append("~ " + line)
    for element, line, ref in zip(previous.elements, previous.lines, previous.refs):
        if not _is_ocr(element) and ref not in seen:
            lines.append("- " + line)
    remaining_ocr = previous_ocr - current_ocr
    for element, line in zip(previous.elements, previous.lines):
        if _is_ocr(element) and remaining_ocr[_strip_index(line)] > 0:
            lines.append("- " + line)
            remaining_ocr[_strip_index(line)] -= 1
    if len(lines) > len(current.lines):
        return DiffResult(is_full=True)
    return DiffResult(lines=lines)


class ScreenTracker:
    def __init__(self) -> None:
        self._assigner = ScreenRefAssigner()
        self._previous: ScreenFrame | None = None

    def observe_frame(
        self, elements: list[ScreenElement], lines: list[str], width: int, height: int
    ) -> DiffResult:
        if not elements or len(elements) != len(lines):
            return DiffResult(is_full=True)
        current = ScreenFrame(
            elements=elements,
            lines=lines,
            refs=self._assigner.assign(elements),
            width=width,
            height=height,
        )
        previous = self._previous
        self._previous = current
        if previous is None:
            return DiffResult(is_full=True)
        return _diff_frames(previous, current)


def render_diff_block(diff_text: str | None) -> str | None:
    if diff_text is None:
        return None
    if not diff_text:
        return UI_DIFF_MARKER + "\n" + NO_SCREEN_CHANGES
    return UI_DIFF_MARKER + "\n" + diff_text
