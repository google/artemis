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

"""Unit tests for the Flash scrub-edge compressor (history redesign §3.2, M1).

Covers the M1 acceptance items: the legacy-equivalent success-path product
(asserted directly since the legacy ``compress_flash_messages`` reference was
removed in M5), the three race regressions (grace recovery, grace exhaustion
without backfill, failed-summary placeholder), the freeze invariant (bytes
behind the scrub edge never change), and depth-1 XML stripping equivalence.
"""

import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from artemis.agents.flash.context_compressor import ScrubEdgeCompressor
from artemis.agents.flash.summarizer import VisualStepSummarizer
from artemis.context import ArtemisContext


@pytest.fixture
def mock_context():
    ctx = Mock(spec=ArtemisContext)
    ctx.data_engine = None
    return ctx


def _initial_observation():
    return HumanMessage(
        content=[
            {"type": "text", "text": "Task: Search flights"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,IMG_0"}},
            {"type": "text", "text": "--- UI Element List ---\nTree 0"},
        ]
    )


def _step(i: int) -> ToolMessage:
    return ToolMessage(
        tool_call_id=f"tc{i}",
        name="click",
        content=[
            {"type": "text", "text": f"Action {i} completed."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,IMG_{i}"}},
            {"type": "text", "text": f"--- UI Element List ---\nTree {i}"},
        ],
    )


def _simulate_turns(compressor, messages, new_messages):
    """Mimic FlashRunner's ordering: compress at turn start, then append."""
    for msg in new_messages:
        compressor.compress(messages)
        messages.append(msg)
    compressor.compress(messages)


def _content_fingerprint(msg) -> str:
    return json.dumps(msg.content, sort_keys=True, default=str)


def _has_image(msg) -> bool:
    return any(
        isinstance(b, dict) and b.get("type") in ("image_url", "image") for b in msg.content
    )


def test_success_path_product_behind_the_scrub_edge(mock_context):
    """With summaries ready in time, every message behind the scrub edge is the
    legacy-shaped product: action text kept, XML stripped, screenshot replaced
    by the ``--- Historical Visual Transition ---`` summary block.

    (Asserted directly; the legacy ``compress_flash_messages`` reference this
    was originally diffed against was removed in M5.) Intended M1 timing
    change: the swap happens at depth K, so the newest K-1 historical steps
    still carry their screenshots.
    """
    summarizer = VisualStepSummarizer(mock_context)
    for i in range(1, 7):
        summarizer._summaries[f"tc{i}"] = f"Visual summary {i}."

    new_messages: list = []
    compressor = ScrubEdgeCompressor(
        summarizer=summarizer,
        prune_history_xml=True,
        image_scrub_depth=3,
        pending_grace_steps=3,
    )
    _simulate_turns(
        compressor, new_messages, [_initial_observation()] + [_step(i) for i in range(1, 7)]
    )

    # Initial observation behind the edge: image silently dropped (no summary
    # job), task text kept, XML stripped.
    assert new_messages[0].content == [{"type": "text", "text": "Task: Search flights"}]

    # Steps 1-4 behind the scrub edge: exact legacy-shaped swap product.
    for i in range(1, 5):
        assert new_messages[i].content == [
            {"type": "text", "text": f"Action {i} completed."},
            {
                "type": "text",
                "text": f"--- Historical Visual Transition ---\nVisual summary {i}.",
            },
        ], f"message {i} diverged from the legacy-shaped product"

    # The live observation is intact.
    assert _has_image(new_messages[6])
    assert "Tree 6" in str(new_messages[6].content)

    # Intended timing change: step 5 (depth 2 < K) keeps its screenshot under
    # the scrub edge; its XML was still stripped at depth 1.
    assert _has_image(new_messages[5])
    assert "Visual summary 5." not in str(new_messages[5].content)
    assert "Tree 5" not in str(new_messages[5].content)


def test_race_pending_at_edge_recovers_within_grace(mock_context):
    """A step whose summary is late keeps its image at the scrub edge and is
    swapped to the summary once it arrives within the grace window."""
    summarizer = VisualStepSummarizer(mock_context)
    summarizer._step_inputs["tc1"] = {"step_number": 1}  # job pending

    messages: list = []
    compressor = ScrubEdgeCompressor(
        summarizer=summarizer,
        image_scrub_depth=3,
        pending_grace_steps=3,
    )
    _simulate_turns(
        compressor, messages, [_initial_observation()] + [_step(i) for i in range(1, 4)]
    )

    # Step 1 sits at the scrub edge with a pending summary: image retained.
    assert _has_image(messages[1])

    # Summary arrives within grace; the next pass applies it.
    summarizer._summaries["tc1"] = "Recovered visual summary 1."
    _simulate_turns(compressor, messages, [_step(4)])

    assert not _has_image(messages[1])
    assert {
        "type": "text",
        "text": "--- Historical Visual Transition ---\nRecovered visual summary 1.",
    } in messages[1].content


def test_race_grace_exhausted_placeholder_never_backfilled(mock_context):
    """After the grace window a pending step becomes a placeholder, and a late
    summary never mutates the frozen message again."""
    summarizer = VisualStepSummarizer(mock_context)
    summarizer._step_inputs["tc1"] = {"step_number": 1}  # pending forever (for now)

    messages: list = []
    compressor = ScrubEdgeCompressor(
        summarizer=summarizer,
        image_scrub_depth=2,
        pending_grace_steps=1,
    )
    _simulate_turns(compressor, messages, [_step(i) for i in range(1, 5)])

    # Depth 4 > K + grace = 3: grace exhausted, placeholder applied.
    step1_text = str(messages[0].content)
    assert not _has_image(messages[0])
    assert "[visual summary pending; evidence at DataEngine step 1]" in step1_text
    frozen_fingerprint = _content_fingerprint(messages[0])

    # The summary arriving later must NOT backfill the frozen message.
    summarizer._summaries["tc1"] = "Too late summary."
    _simulate_turns(compressor, messages, [_step(5)])
    compressor.compress(messages)

    assert _content_fingerprint(messages[0]) == frozen_fingerprint
    assert "Too late summary." not in str(messages[0].content)


def test_race_failed_summary_becomes_unavailable_placeholder(mock_context):
    """A failed summary is replaced by an unavailable placeholder at the scrub
    edge without waiting out the grace window."""
    summarizer = VisualStepSummarizer(mock_context)
    summarizer._step_inputs["tc1"] = {"step_number": 1}
    summarizer._failed.add("tc1")

    messages: list = []
    compressor = ScrubEdgeCompressor(
        summarizer=summarizer,
        image_scrub_depth=2,
        pending_grace_steps=5,
    )
    _simulate_turns(compressor, messages, [_step(1), _step(2), _step(3)])

    assert not _has_image(messages[0])
    assert "[visual summary unavailable; evidence at DataEngine step 1]" in str(
        messages[0].content
    )


def test_freeze_invariant_bytes_never_change(mock_context):
    """Once a message crosses the scrub edge its content bytes never change,
    across many turns and late-arriving summaries."""
    summarizer = VisualStepSummarizer(mock_context)
    # Odd steps have summaries ready from the start; even steps stay pending.
    for i in range(1, 11):
        summarizer._step_inputs[f"tc{i}"] = {"step_number": i}
        if i % 2 == 1:
            summarizer._summaries[f"tc{i}"] = f"Ready summary {i}."

    messages: list = []
    compressor = ScrubEdgeCompressor(
        summarizer=summarizer,
        image_scrub_depth=2,
        pending_grace_steps=1,
    )

    frozen_fingerprints: dict[int, str] = {}

    def check_frozen():
        for idx in compressor._frozen:
            fingerprint = _content_fingerprint(messages[idx])
            if idx in frozen_fingerprints:
                assert frozen_fingerprints[idx] == fingerprint, (
                    f"frozen message {idx} mutated"
                )
            else:
                frozen_fingerprints[idx] = fingerprint

    for i in range(1, 11):
        compressor.compress(messages)
        check_frozen()
        messages.append(AIMessage(content=f"Thinking about step {i}."))
        messages.append(_step(i))
        if i == 6:
            # Late summaries for already-frozen even steps: must be ignored.
            summarizer._summaries["tc2"] = "Late summary 2."
            summarizer._summaries["tc4"] = "Late summary 4."
    compressor.compress(messages)
    check_frozen()

    assert len(frozen_fingerprints) >= 6
    # A frozen pending step carries the placeholder, not the late summary.
    assert "Late summary 2." not in " ".join(str(m.content) for m in messages)


def test_xml_depth1_strip_matches_legacy_semantics(mock_context):
    """Depth-1 XML stripping keeps combined-block prefixes and the live list."""
    messages: list = []
    compressor = ScrubEdgeCompressor(summarizer=None, prune_history_xml=True)

    combined = ToolMessage(
        tool_call_id="tc1",
        name="click",
        content=[
            {
                "type": "text",
                "text": "Tapped Settings.\n--- UI Element List ---\n[huge tree]",
            },
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,A"}},
        ],
    )
    _simulate_turns(compressor, messages, [combined, _step(2)])

    # Historical combined block: prefix retained, tree stripped (the exact
    # legacy ``compress_flash_messages`` product for this input).
    assert messages[0].content[0] == {"type": "text", "text": "Tapped Settings."}
    # Live observation keeps its UI list untouched.
    assert "Tree 2" in str(messages[1].content)


def test_prune_history_xml_disabled_keeps_ui_lists(mock_context):
    """With the switch off, UI lists survive both depth-1 and freeze scrubs."""
    summarizer = VisualStepSummarizer(mock_context)
    summarizer._summaries["tc1"] = "Summary 1."

    messages: list = []
    compressor = ScrubEdgeCompressor(
        summarizer=summarizer,
        prune_history_xml=False,
        image_scrub_depth=2,
        pending_grace_steps=0,
    )
    _simulate_turns(compressor, messages, [_step(1), _step(2), _step(3)])

    # Step 1 froze (summary applied) but its UI list is preserved.
    assert not _has_image(messages[0])
    assert "Tree 1" in str(messages[0].content)
    assert "Summary 1." in str(messages[0].content)


def test_untracked_messages_are_never_touched(mock_context):
    """Injected instructions and AI turns pass through untouched; a frozen
    image-only tool message falls back to non-empty content."""
    messages: list = [
        HumanMessage(content="[REAL-TIME INJECTED INSTRUCTION from user]: stay put"),
    ]
    compressor = ScrubEdgeCompressor(summarizer=None, image_scrub_depth=2)

    image_only = ToolMessage(
        tool_call_id="tc1",
        name="click",
        content=[{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,A"}}],
    )
    _simulate_turns(
        compressor,
        messages,
        [image_only, AIMessage(content="thinking"), _step(2), _step(3)],
    )

    assert messages[0].content == "[REAL-TIME INJECTED INSTRUCTION from user]: stay put"
    assert messages[2].content == "thinking"
    # Image-only tool message froze with the defensive fallback text.
    assert messages[1].content == [{"type": "text", "text": "Action completed."}]
