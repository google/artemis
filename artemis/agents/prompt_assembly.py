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

"""Helpers for assembling tool references into otherwise-static prompts.

Prompts stay hand-written text; only the spots that mention tools are assembled:

* **Enumeration slots** -- lists like ``(`click`, `swipe`, ... , or `wait_for_delay`)``
  are re-rendered from the actually-available tool set, so an absent tool never
  appears. With the full tool set the rendering reproduces the original wording
  byte-for-byte (ordering, backticks, Oxford ``or``/``and``), which is what the
  regression tests pin.
* **Instruction segments** -- self-contained blocks that teach one tool (e.g. the
  ``manage_app`` app-launching rule) are included only while that tool exists.

An unavailable tool therefore leaves no trace in the prompt at all -- the executable
definition of "an unimplemented tool must cost the model nothing".
"""

from collections.abc import Iterable, Sequence, Set

__all__ = ["gate_segment", "render_tool_enum", "resolve_available"]


def render_tool_enum(
    tools: Sequence[str],
    available: Set[str],
    final_sep: str | None = None,
) -> str:
    """Renders an ordered, backticked tool enumeration limited to available tools.

    Args:
        tools: Tool names in the exact order the prompt historically used.
        available: Tools that currently exist.
        final_sep: ``"or"``/``"and"`` to join the last item Oxford-style
            (``"`a`, `b`, or `c`"``); ``None`` for a plain comma list.

    Returns:
        The rendered enumeration, or ``""`` when none of the tools are available.
    """
    ticked = [f"`{name}`" for name in tools if name in available]
    if not ticked:
        return ""
    if len(ticked) == 1:
        return ticked[0]
    if final_sep is None:
        return ", ".join(ticked)
    if len(ticked) == 2:
        return f"{ticked[0]} {final_sep} {ticked[1]}"
    return ", ".join(ticked[:-1]) + f", {final_sep} " + ticked[-1]


def gate_segment(segment: str, available: Set[str], *tools: str) -> str:
    """Returns ``segment`` when every tool it teaches is available, else ``""``."""
    if all(name in available for name in tools):
        return segment
    return ""


def resolve_available(available: Iterable[str] | None, full_set: Set[str]) -> Set[str]:
    """Normalizes an optional availability argument.

    ``None`` means "everything" -- callers that have no actuator wired yet keep
    today's output unchanged. Passing an explicit set opts into assembly.
    """
    if available is None:
        return full_set
    return frozenset(available)
