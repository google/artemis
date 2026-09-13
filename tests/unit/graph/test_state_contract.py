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

"""Phase 1 contract tests: explicit State + per-node visibility manifests."""

import pytest
from pydantic import ValidationError

from artemis.graph.state import State
from artemis.graph.visibility import (
    NODE_VISIBILITY,
    StateView,
    VisibilityError,
    check_update,
    strict_state,
    validate_manifest,
)


def test_initial_state_round_trips_through_channel_values():
    """sdk/agent.py rebuilds State(**values) from the astream values stream;
    with extra="forbid" every dumped key must construct back cleanly."""
    state = State.initial("test goal")
    rebuilt = State(**state.model_dump())
    assert rebuilt.initial_goal == "test goal"
    assert rebuilt.subagent_calls == []


def test_unknown_constructor_key_is_rejected():
    with pytest.raises(ValidationError):
        State(initial_goal="g", ghost_key=1)


def test_unknown_attribute_write_is_rejected():
    state = State.initial("g")
    with pytest.raises(ValueError):
        state.ui_tree = []  # the historical ghost key must never come back


def test_deleted_dead_fields_are_gone():
    for dead in (
        "messages",
        "validator_messages",
        "remaining_steps",
        "focused_app_info",
        "device_date",
        "subgoal_plan",
        "operator_tactical_plan",
        "complete_subgoals_by_ids",
        "current_agent",
        "operator_replan_reason",
        "check_feedback",
    ):
        assert dead not in State.model_fields, dead


def test_manifest_only_references_declared_fields():
    assert validate_manifest() == []


def test_state_view_allows_declared_and_blocks_undeclared_reads():
    state = State.initial("g")
    view = StateView(state, "operator")
    assert view.initial_goal == "g"  # declared read
    with pytest.raises(VisibilityError):
        _ = view.exit_settlement_route  # not in operator's manifest


def test_state_view_blocks_undeclared_mutation():
    state = State.initial("g")
    view = StateView(state, "summarizer")  # summarizer declares no writes
    with pytest.raises(VisibilityError):
        view.structured_decisions = "[]"


def test_state_view_passes_methods_through():
    view = StateView(State.initial("g"), "operator")
    dumped = view.model_dump()
    assert dumped["initial_goal"] == "g"


def test_check_update_flags_undeclared_write():
    with pytest.raises(VisibilityError):
        check_update("summarizer", {"structured_decisions": "[]"})
    check_update("operator", {"structured_decisions": "[]"})  # declared: ok
    check_update("operator", None)  # empty update: ok


def test_strict_state_is_identity_when_disabled(monkeypatch):
    monkeypatch.delenv("ARTEMIS_STRICT_STATE", raising=False)
    state = State.initial("g")
    assert strict_state(state, "operator") is state


def test_strict_state_wraps_when_enabled(monkeypatch):
    monkeypatch.setenv("ARTEMIS_STRICT_STATE", "1")
    state = State.initial("g")
    view = strict_state(state, "operator")
    assert isinstance(view, StateView)
