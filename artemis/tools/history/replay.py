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

"""replay_steps: full replay of a step range, exactly as the executing agent saw it.

Each step is rendered with :func:`~artemis.utils.task_tree.render_step_replay`
(``[Screen]`` description, reasoning, every non-internal tool call with name /
arguments / result, the planned action, safety-net interception and the
execution result). Two loose bounds keep one tool result finite (config
``agent.memory.replay``): at most ``max_steps`` steps per call, and a
``max_tokens`` budget applied by dropping *whole trailing steps* — a replayed
step is never cut mid-sentence. Screenshots are deliberately not part of the
replay; ``get_step_screenshot`` fetches them one at a time.
"""

from __future__ import annotations

from typing import Any

from artemis.utils.task_tree import render_step_replay

DEFAULT_MAX_STEPS = 5
DEFAULT_MAX_TOKENS = 12000


def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def replay_steps_text(
    reader: Any,
    start_step: Any,
    end_step: Any = None,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Replays steps ``start_step``..``end_step`` (inclusive, 1-based) from ``reader``."""
    if reader is None:
        return "Error: no execution history available."
    try:
        start = int(start_step)
        end = start if end_step is None else int(end_step)
    except (TypeError, ValueError):
        return (
            f"Error: start_step and end_step must be integers, got {start_step!r} and {end_step!r}."
        )
    if start > end:
        start, end = end, start

    max_steps = max(1, int(max_steps or DEFAULT_MAX_STEPS))
    notes: list[str] = []
    if end - start + 1 > max_steps:
        end = start + max_steps - 1
        notes.append(
            f"(Only {max_steps} steps are replayed per call: showing Steps {start}–{end}."
            f" Call again from Step {end + 1} for the rest.)"
        )

    try:
        steps: list[dict] = reader.get_agent_friendly_steps_in_range(start, end) or []
    except Exception as e:
        return f"Error loading steps {start}–{end}: {e}"
    if not steps:
        if start == end:
            return f"Error: step {start} not found."
        return f"Error: no recorded steps in range {start}–{end}."

    budget_tokens = max(1, int(max_tokens or DEFAULT_MAX_TOKENS))
    rendered: list[str] = []
    shown: list[dict] = []
    used = 0
    dropped = 0
    for step in steps:
        if dropped:
            dropped += 1
            continue
        try:
            text = render_step_replay(step)
        except Exception as e:
            return f"Error rendering steps {start}–{end}: {e}"
        cost = _estimate_tokens(text) + 1
        # The first step is always replayed; afterwards the budget drops whole
        # trailing steps rather than truncating a step in the middle.
        if rendered and used + cost > budget_tokens:
            dropped = 1
            continue
        rendered.append(text)
        shown.append(step)
        used += cost

    if dropped:
        last_shown = shown[-1].get("step_number", start)
        first_shown = shown[0].get("step_number", start)
        notes.append(
            f"(Token budget reached: Steps {first_shown}–{last_shown} replayed;"
            f" {dropped} more step(s) in the range not shown."
            f" Call again from Step {last_shown + 1}.)"
        )

    text = "\n\n".join(rendered)
    if notes:
        text += "\n\n" + "\n".join(notes)
    return text
