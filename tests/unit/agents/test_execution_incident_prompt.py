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

"""The Operator-facing execution incident block and the incident record."""

from types import SimpleNamespace

import pytest

from artemis.agents.operator.prompts import (
    EXECUTION_INCIDENT_MARKER,
    ExecutionIncidentPromptComponent,
    PromptBuilder,
    render_closed_incident,
    render_execution_incident,
)
from artemis.agents.validator.categories import ValidationErrorCategory
from artemis.agents.validator.incidents import (
    KIND_EXEC_ERROR,
    KIND_SAFETY_NET,
    ExecutionIncident,
    open_incident,
)

STEPS = [
    {
        "step_number": 11,
        "action_taken": [{"action": "click", "coordinates": [540, 1800], "target_text": "player"}],
        "last_execution_result": {"status": "success"},
    },
    {
        "step_number": 12,
        "action_taken": [{"action": "click", "target_text": "Skip"}],
        "last_execution_result": {"status": "failed"},
    },
]


def _disappeared(consecutive=1, **overrides):
    incident = open_incident(
        previous=None,
        kind=KIND_SAFETY_NET,
        category=ValidationErrorCategory.TARGET_DISAPPEARED,
        reason="Target element 'Skip' was not found on the screen.",
        action_item={
            "action": "tap",
            "coordinates": [950, 288],
            "normalized_coordinates": [880, 120],
            "target_text": "Skip",
            "attempts": ["Pre-execution validation failed: ..."],
        },
        action_description="Tapped 'Skip' at [950, 288]",
        step_number=12,
    )
    incident["consecutive_failures"] = consecutive
    incident.update(overrides)
    return incident


# --- record -------------------------------------------------------------------------


def test_open_incident_strips_internal_keys_and_starts_at_one():
    incident = _disappeared()
    assert incident["consecutive_failures"] == 1
    assert "attempts" not in incident["action"]
    assert incident["category"] == "target_disappeared"
    assert incident["burst_size"] == 1 and incident["action_index"] == 0


def test_open_incident_continues_the_previous_count():
    previous = _disappeared(consecutive=2)
    incident = open_incident(
        previous=previous,
        kind=KIND_EXEC_ERROR,
        category=ValidationErrorCategory.GENERAL,
        reason="Error: tap rejected",
        action_item={"action": "tap"},
        action_description="Tapped element",
    )
    assert incident["consecutive_failures"] == 3
    assert ExecutionIncident.from_dict(incident).is_burst is False
    assert ExecutionIncident.from_dict(None) is None
    assert ExecutionIncident.from_dict({"kind": "safety_net", "unknown_key": 1}) is not None


# --- rendering ----------------------------------------------------------------------


def test_render_disappeared_names_target_trigger_and_burst_exception():
    text = render_execution_incident(_disappeared(), STEPS)
    assert text.startswith(EXECUTION_INCIDENT_MARKER)
    assert "Opened at Step 12; consecutive failed turns: 1." in text
    assert "was NOT executed. The pre-execution safety net refused it" in text
    # Recorded target with normalized coordinates and text.
    assert 'normalized [880, 120] "Skip"' in text
    # The last successful action before the incident is the trigger candidate.
    assert "Your last successfully executed action was `Tapped 'player' at [540, 1800]`" in text
    assert "(Step 11)" in text
    # Facts and evidence only: the response protocol is stated once, in the
    # static system prompt, so the per-turn block never restates it.
    assert "How to respond" not in text
    assert "settle the original intent" not in text
    assert "e.g." not in text
    # No escalation ladder: the Operator keeps its freedom.
    assert "Escalation" not in text
    assert "ask_diagnoser" not in text


def test_render_shifted_and_occupied_evidence():
    shifted = _disappeared(
        category="target_shifted",
        evidence={"new_location": [500, 640], "new_bounds": [400, 600, 600, 680]},
    )
    text = render_execution_incident(shifted, STEPS)
    assert "has moved at normalized [500, 640] (bounds [400, 600, 600, 680])" in text

    occupied = _disappeared(category="target_occupied", evidence={"occupant": "'Sign Up' button"})
    text = render_execution_incident(occupied, STEPS)
    assert "now covered by 'Sign Up' button" in text


def test_render_burst_abort_and_exec_error():
    burst = _disappeared(kind=KIND_EXEC_ERROR, category="general", burst_size=3, action_index=1)
    burst["reason"] = "Error: tap rejected"
    text = render_execution_incident(burst, STEPS)
    assert "action 2 of your 3-action fast burst" in text
    assert "The 1 action(s) after it were NOT executed" in text

    plain = _disappeared(kind=KIND_EXEC_ERROR, category="general")
    text = render_execution_incident(plain, STEPS)
    assert "was dispatched, but the device/executor reported" in text


def test_render_without_history_omits_the_trigger_hint():
    text = render_execution_incident(_disappeared(), [])
    assert "last successfully executed action" not in text
    assert 'Its recorded target was normalized [880, 120] "Skip".' in text


# --- component ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_component_renders_only_while_an_incident_is_open():
    builder = PromptBuilder()
    ctx = SimpleNamespace(data_engine=None)
    await ExecutionIncidentPromptComponent()(
        builder, SimpleNamespace(open_incident=None), ctx, steps=STEPS
    )
    assert builder.human_parts == []

    await ExecutionIncidentPromptComponent()(
        builder, SimpleNamespace(open_incident=_disappeared()), ctx, steps=STEPS
    )
    assert len(builder.human_parts) == 1
    # Its own text part: the transcript scrub keeps it verbatim across turns.
    assert builder.human_parts[0].startswith(EXECUTION_INCIDENT_MARKER)


def test_render_closed_notice_settles_the_intent():
    closed = dict(_disappeared(), closed_at_step=15)
    text = render_closed_incident(closed)
    assert text.startswith("--- Execution Incident (CLOSED at Step 15) ---")
    assert "opened at Step 12 on `Tapped 'Skip' at [950, 288]`" in text
    assert "already served, still pending, or no longer needed" in text


@pytest.mark.asyncio
async def test_component_renders_closed_notice_once_no_incident_is_open():
    builder = PromptBuilder()
    ctx = SimpleNamespace(data_engine=None)
    state = SimpleNamespace(
        open_incident=None, last_closed_incident=dict(_disappeared(), closed_at_step=15)
    )
    await ExecutionIncidentPromptComponent()(builder, state, ctx, steps=STEPS)
    assert len(builder.human_parts) == 1
    assert builder.human_parts[0].startswith("--- Execution Incident (CLOSED at Step 15) ---")

    # An open incident takes precedence over a stale closed notice.
    builder = PromptBuilder()
    state = SimpleNamespace(
        open_incident=_disappeared(), last_closed_incident=dict(_disappeared(), closed_at_step=15)
    )
    await ExecutionIncidentPromptComponent()(builder, state, ctx, steps=STEPS)
    assert builder.human_parts[0].startswith(EXECUTION_INCIDENT_MARKER)
