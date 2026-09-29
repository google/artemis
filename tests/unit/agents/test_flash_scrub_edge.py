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

"""Tests for screenshot summaries, text cleanup, and frozen history.

Covers pending-summary recovery and expiry, failed-summary placeholders,
independent text and image depths, ephemeral-block remapping, and stable
content after both cleanup passes.
"""

import json
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from artemis.agents.flash.context_compressor import ScrubEdgeCompressor
from artemis.agents.flash.summarizer import VisualStepSummarizer
from artemis.context import ArtemisContext


@pytest.fixture(autouse=True)
def isolate_summarizer_model(monkeypatch):
    """Keep real summarizer behavior without constructing a provider client."""
    model = Mock()
    model.ainvoke = AsyncMock(
        side_effect=AssertionError("Configure a model response before invoking the summarizer")
    )
    factory = Mock(return_value=model)
    monkeypatch.setattr("artemis.agents.flash.summarizer.get_llm", factory)
    monkeypatch.setattr("artemis.agents.flash.summarizer.get_google_llm", factory)


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
    return any(isinstance(b, dict) and b.get("type") in ("image_url", "image") for b in msg.content)


def test_success_path_product_behind_the_scrub_edge(mock_context):
    """With summaries ready in time, every message behind the scrub edge is the
    legacy-shaped product: action text kept, XML stripped, screenshot replaced
    by the ``--- Historical Visual Transition ---`` summary block.

    (Asserted directly; the legacy ``compress_flash_messages`` reference this
    was originally diffed against was removed in M5.) The swap happens at
    depth K, so the newest K-1 historical steps still carry their screenshots;
    their UI lists leave earlier, at the shallow text edge (depth 1), so only
    the live observation ever carries an indexed element list.
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

    # Step 5 (depth 2 < K) is past the text edge only: screenshot intact,
    # UI list already gone, summary not yet swapped in.
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
    assert "[visual summary unavailable; evidence at DataEngine step 1]" in str(messages[0].content)


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
                assert frozen_fingerprints[idx] == fingerprint, f"frozen message {idx} mutated"
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


def test_strip_at_the_edge_keeps_combined_block_prefix_and_live_list(mock_context):
    """The XML strip at the edge keeps combined-block prefixes and the live list."""
    messages: list = []
    compressor = ScrubEdgeCompressor(summarizer=None, prune_history_xml=True, image_scrub_depth=2)

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

    # Combined block at the edge: prefix retained, tree stripped (the exact
    # legacy ``compress_flash_messages`` product for this input).
    assert messages[0].content[0] == {"type": "text", "text": "Tapped Settings."}
    # Live observation keeps its UI list untouched.
    assert "Tree 2" in str(messages[1].content)


def test_imageless_ui_list_carrier_is_stripped_as_soon_as_a_newer_observation_exists(
    mock_context,
):
    """A step whose screenshot failed still carries a UI list; it follows the
    text edge like any observation: while it is the newest observation it keeps
    the list, and it is stripped as soon as a newer observation exists — even
    when that newer one has no image either — so only the live observation ever
    carries an indexed element list."""
    messages: list = []
    compressor = ScrubEdgeCompressor(summarizer=None, image_scrub_depth=2)

    def imageless(n: int) -> ToolMessage:
        return ToolMessage(
            tool_call_id=f"tc{n}",
            name="click",
            content=[
                {"type": "text", "text": f"Tapped {n}, screenshot failed."},
                {"type": "text", "text": f"--- UI Element List ---\nTree {n}"},
            ],
        )

    _simulate_turns(compressor, messages, [imageless(1)])
    assert "Tree 1" in str(messages[0].content)  # newest observation: kept
    _simulate_turns(compressor, messages, [imageless(2)])  # screenshots keep failing
    assert messages[0].content == [{"type": "text", "text": "Tapped 1, screenshot failed."}]
    assert "Tree 2" in str(messages[1].content)
    _simulate_turns(compressor, messages, [_step(3)])
    assert messages[1].content == [{"type": "text", "text": "Tapped 2, screenshot failed."}]
    assert "Tree 3" in str(messages[2].content)  # live observation untouched


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
    assert messages[1].content == [{"type": "text", "text": "Action dispatched."}]


def test_depth_override_tightens_once_and_deeper_pass_never_backfills(mock_context):
    """A per-call depth override: a shallower pass scrubs the extra depths in
    one go; a deeper pass afterwards touches nothing already frozen."""
    summarizer = VisualStepSummarizer(mock_context)
    for i in range(1, 9):
        summarizer._step_inputs[f"tc{i}"] = {"step_number": i}
        summarizer._summaries[f"tc{i}"] = f"Summary {i}."
    messages: list = [_initial_observation()]
    compressor = ScrubEdgeCompressor(summarizer=summarizer, image_scrub_depth=3)

    for i in range(1, 9):
        compressor.compress(messages, image_scrub_depth=6)
        messages.append(_step(i))
    compressor.compress(messages, image_scrub_depth=6)
    with_images = [m for m in messages if _has_image(m)]
    # Depth 6 with the live step at depth 1: tc4..tc7 (depths 5..2) keep
    # their screenshots, tc3 (depth 6) is scrubbed.
    assert [m.tool_call_id for m in with_images] == ["tc4", "tc5", "tc6", "tc7", "tc8"]

    compressor.compress(messages, image_scrub_depth=3)  # tighten: three more scrubbed at once
    with_images = [m for m in messages if _has_image(m)]
    assert [m.tool_call_id for m in with_images] == ["tc7", "tc8"]
    frozen = {idx: _content_fingerprint(messages[idx]) for idx in compressor._frozen}

    compressor.compress(messages, image_scrub_depth=6)  # relax: nothing restored
    assert [m.tool_call_id for m in messages if _has_image(m)] == ["tc7", "tc8"]
    assert {idx: _content_fingerprint(messages[idx]) for idx in frozen} == frozen


# ---------------------------------------------------------------------------
# Screenshot replacement, ephemeral blocks, and frozen content
# ---------------------------------------------------------------------------


def _labelled_step(i: int, *, footer: str | None = None) -> ToolMessage:
    """A step in the Flash observation shape with the screenshot label block."""
    blocks = [
        {"type": "text", "text": f"Action {i} completed."},
        {"type": "text", "text": "--- Current Screenshot ---"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,IMG_{i}"}},
        {"type": "text", "text": f"--- UI Element List ---\nTree {i}"},
    ]
    if footer is not None:
        blocks.append({"type": "text", "text": footer})
    return ToolMessage(tool_call_id=f"tc{i}", name="click", content=blocks)


def test_summary_takes_the_images_place_and_the_label_leaves_with_it(mock_context):
    """(i) The visual summary block sits exactly where the image block was —
    not appended after whatever followed — and no ``--- Current Screenshot ---``
    label is left dangling above it."""
    summarizer = VisualStepSummarizer(mock_context)
    summarizer._summaries["tc1"] = "Summary 1."
    messages: list = []
    compressor = ScrubEdgeCompressor(summarizer=summarizer, image_scrub_depth=2)
    _simulate_turns(
        compressor, messages, [_labelled_step(1, footer="Footer note 1"), _labelled_step(2)]
    )

    assert messages[0].content == [
        {"type": "text", "text": "Action 1 completed."},
        {"type": "text", "text": "--- Historical Visual Transition ---\nSummary 1."},
        {"type": "text", "text": "Footer note 1"},
    ]
    assert "--- Current Screenshot ---" not in str(messages[0].content)
    # The live step keeps its label above its screenshot.
    assert messages[1].content[1] == {"type": "text", "text": "--- Current Screenshot ---"}


def test_ephemeral_blocks_vanish_at_the_text_edge_and_not_before(mock_context):
    """(ii) Blocks listed under ``EPHEMERAL_BLOCKS_KEY`` survive only while
    the message is the live observation; at the text edge (a newer observation
    exists) they are deleted together with the UI list, the screenshot stays
    until depth K; the consumed indices are cleared at the text edge."""
    from artemis.memory.transcript import EPHEMERAL_BLOCKS_KEY, mark_ephemeral

    summarizer = VisualStepSummarizer(mock_context)
    summarizer._summaries["tc1"] = "Summary 1."
    step1 = ToolMessage(
        tool_call_id="tc1",
        name="click",
        content=[
            {"type": "text", "text": "Action 1 completed."},
            {"type": "text", "text": "[reminder: state your reasoning]"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,IMG_1"}},
            {"type": "text", "text": "--- UI Element List ---\nTree 1"},
            {"type": "text", "text": "[hint: valid this turn only]"},
        ],
    )
    mark_ephemeral(step1, [1, 4])

    messages: list = []
    compressor = ScrubEdgeCompressor(summarizer=summarizer, image_scrub_depth=3)
    _simulate_turns(compressor, messages, [step1])
    # Live observation: everything, ephemeral blocks included, still there.
    assert "[reminder: state your reasoning]" in str(messages[0].content)
    assert "[hint: valid this turn only]" in str(messages[0].content)
    assert messages[0].additional_kwargs[EPHEMERAL_BLOCKS_KEY] == [1, 4]

    _simulate_turns(compressor, messages, [_step(2)])  # step 1 reaches depth 2
    # Text edge: notices and UI list gone, the screenshot stays (depth 2 < K).
    assert messages[0].content == [
        {"type": "text", "text": "Action 1 completed."},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,IMG_1"}},
    ]
    assert EPHEMERAL_BLOCKS_KEY not in messages[0].additional_kwargs

    _simulate_turns(compressor, messages, [_step(3)])  # step 1 reaches depth 3
    assert messages[0].content == [
        {"type": "text", "text": "Action 1 completed."},
        {"type": "text", "text": "--- Historical Visual Transition ---\nSummary 1."},
    ]
    assert EPHEMERAL_BLOCKS_KEY not in messages[0].additional_kwargs


def test_grace_does_the_text_edits_at_k_once_and_only_the_image_swap_waits(mock_context):
    """A pending summary keeps the screenshot (and its label) inside the grace
    window, but the strip and the ephemeral deletion happen at K exactly once;
    the later swap puts the summary where the image was and freezes."""
    from artemis.memory.transcript import mark_ephemeral

    summarizer = VisualStepSummarizer(mock_context)
    summarizer._step_inputs["tc1"] = {"step_number": 1}  # pending
    step1 = _labelled_step(1, footer="[per-turn notice]")
    mark_ephemeral(step1, [4])

    messages: list = []
    compressor = ScrubEdgeCompressor(
        summarizer=summarizer, image_scrub_depth=2, pending_grace_steps=3
    )
    _simulate_turns(compressor, messages, [step1, _labelled_step(2)])
    assert messages[0].content == [
        {"type": "text", "text": "Action 1 completed."},
        {"type": "text", "text": "--- Current Screenshot ---"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,IMG_1"}},
    ]
    after_text_edits = _content_fingerprint(messages[0])
    _simulate_turns(compressor, messages, [_labelled_step(3)])  # still pending, in grace
    assert _content_fingerprint(messages[0]) == after_text_edits

    summarizer._summaries["tc1"] = "Late but in time."
    _simulate_turns(compressor, messages, [_labelled_step(4)])
    assert messages[0].content == [
        {"type": "text", "text": "Action 1 completed."},
        {"type": "text", "text": "--- Historical Visual Transition ---\nLate but in time."},
    ]
    assert 0 in compressor._frozen


def test_messages_past_the_edge_are_byte_identical_on_every_later_pass(mock_context):
    """(iii) Once a message is past the edge, compressing again — with new
    turns appended, depths relaxed or tightened, summaries arriving late —
    leaves it byte-identical. Each pass rewrites exactly the two messages
    that reached an edge (the text edge at depth 2, the screenshot edge at
    K) and nothing older."""
    summarizer = VisualStepSummarizer(mock_context)
    for i in range(1, 13):
        summarizer._step_inputs[f"tc{i}"] = {"step_number": i}
        summarizer._summaries[f"tc{i}"] = f"Summary {i}."
    messages: list = []
    compressor = ScrubEdgeCompressor(summarizer=summarizer, image_scrub_depth=3)

    previous: list[str] = []
    for i in range(1, 13):
        messages.append(_labelled_step(i))
        compressor.compress(messages)
        current = [_content_fingerprint(m) for m in messages]
        # Everything before the messages that reached an edge is unchanged.
        changed = [idx for idx, fp in enumerate(previous) if current[idx] != fp]
        # The message reaching depth 2 (text edge) is index len-1 of the previous
        # pass's list, the one reaching depth 3 (screenshot edge) is len-2; the
        # new live message is not in that list yet.
        expected = [idx for idx in (len(previous) - 2, len(previous) - 1) if idx >= 0]
        assert changed == expected, changed
        previous = current

    # No new messages: a repeated pass, relaxed or not, is a no-op byte for byte.
    compressor.compress(messages)
    compressor.compress(messages, image_scrub_depth=6)
    assert [_content_fingerprint(m) for m in messages] == previous
    # Tightening rewrites exactly the message at the new depth, nothing older.
    compressor.compress(messages, image_scrub_depth=2)
    changed = [i for i, fp in enumerate(previous) if _content_fingerprint(messages[i]) != fp]
    assert changed == [len(messages) - 2]


def test_text_edge_deeper_than_screenshot_edge_keeps_the_edges_independent(mock_context):
    """xml_scrub_depth=4 with K=3: the screenshot is resolved at depth 3 while
    the UI list stays until the message is deeper than 4; the message is only
    frozen once both edits have happened."""
    summarizer = VisualStepSummarizer(mock_context)
    for i in range(1, 8):
        summarizer._summaries[f"tc{i}"] = f"Summary {i}."
    messages: list = []
    compressor = ScrubEdgeCompressor(summarizer=summarizer, image_scrub_depth=3, xml_scrub_depth=4)
    _simulate_turns(compressor, messages, [_labelled_step(i) for i in range(1, 4)])
    # Step 1 at depth 3: image swapped, label gone, UI list still there.
    assert "Summary 1." in str(messages[0].content)
    assert not _has_image(messages[0])
    assert "--- Current Screenshot ---" not in str(messages[0].content)
    assert "Tree 1" in str(messages[0].content)

    _simulate_turns(compressor, messages, [_labelled_step(4), _labelled_step(5)])
    # Step 1 at depth 5 (> 4): UI list gone now; the summary is untouched.
    assert "Tree 1" not in str(messages[0].content)
    assert messages[0].content == [
        {"type": "text", "text": "Action 1 completed."},
        {"type": "text", "text": "--- Historical Visual Transition ---\nSummary 1."},
    ]
    frozen = _content_fingerprint(messages[0])
    _simulate_turns(compressor, messages, [_labelled_step(6), _labelled_step(7)])
    assert _content_fingerprint(messages[0]) == frozen


# ---------------------------------------------------------------------------
# Ephemeral indices stay aligned when the image edge is shallower than the
# text edge (a rewrite before the text edit shifts the later blocks)
# ---------------------------------------------------------------------------


def _step_with_notices(i: int) -> ToolMessage:
    """Flash observation shape with one ephemeral notice on each side of the
    screenshot and a body block *after* the notice that follows the image, so
    a stale index would delete the body instead of the notice."""
    from artemis.memory.transcript import mark_ephemeral

    msg = ToolMessage(
        tool_call_id=f"tc{i}",
        name="click",
        content=[
            {"type": "text", "text": f"Action {i} completed."},
            {"type": "text", "text": f"[hint {i}: before the image]"},
            {"type": "text", "text": "--- Current Screenshot ---"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,IMG_{i}"}},
            {"type": "text", "text": f"--- UI Element List ---\nTree {i}"},
            {"type": "text", "text": f"[reminder {i}: after the image]"},
            {"type": "text", "text": f"Body note {i}"},
        ],
    )
    mark_ephemeral(msg, [1, 5])
    return msg


def test_image_edge_before_text_edge_remaps_ephemeral_indices_to_the_summary_layout(
    mock_context,
):
    """K=3 with xml_scrub_depth=4: the summary swap at depth 3 removes the
    label block, so the notice after the image moves up by one. The stored
    indices must follow it; the later text pass then deletes both notices and
    leaves the body block intact."""
    from artemis.memory.transcript import EPHEMERAL_BLOCKS_KEY

    summarizer = VisualStepSummarizer(mock_context)
    for i in range(1, 6):
        summarizer._summaries[f"tc{i}"] = f"Summary {i}."
    messages: list = []
    compressor = ScrubEdgeCompressor(summarizer=summarizer, image_scrub_depth=3, xml_scrub_depth=4)
    _simulate_turns(compressor, messages, [_step_with_notices(i) for i in range(1, 4)])

    # Image pass (depth 3): label + image -> summary; notices still present.
    assert messages[0].content == [
        {"type": "text", "text": "Action 1 completed."},
        {"type": "text", "text": "[hint 1: before the image]"},
        {"type": "text", "text": "--- Historical Visual Transition ---\nSummary 1."},
        {"type": "text", "text": "--- UI Element List ---\nTree 1"},
        {"type": "text", "text": "[reminder 1: after the image]"},
        {"type": "text", "text": "Body note 1"},
    ]
    assert messages[0].additional_kwargs[EPHEMERAL_BLOCKS_KEY] == [1, 4]

    _simulate_turns(compressor, messages, [_step_with_notices(4), _step_with_notices(5)])
    # Text pass (depth 5 > 4): notices and UI list gone, body block intact.
    assert messages[0].content == [
        {"type": "text", "text": "Action 1 completed."},
        {"type": "text", "text": "--- Historical Visual Transition ---\nSummary 1."},
        {"type": "text", "text": "Body note 1"},
    ]
    assert EPHEMERAL_BLOCKS_KEY not in messages[0].additional_kwargs
    assert 0 in compressor._frozen


def test_image_edge_before_text_edge_remaps_when_the_image_is_dropped(mock_context):
    """Same ordering with no summary job at all: label and image both leave
    (two blocks removed), the notice after the image moves up by two."""
    from artemis.memory.transcript import EPHEMERAL_BLOCKS_KEY

    messages: list = []
    compressor = ScrubEdgeCompressor(summarizer=None, image_scrub_depth=3, xml_scrub_depth=4)
    _simulate_turns(compressor, messages, [_step_with_notices(i) for i in range(1, 4)])

    assert messages[0].content == [
        {"type": "text", "text": "Action 1 completed."},
        {"type": "text", "text": "[hint 1: before the image]"},
        {"type": "text", "text": "--- UI Element List ---\nTree 1"},
        {"type": "text", "text": "[reminder 1: after the image]"},
        {"type": "text", "text": "Body note 1"},
    ]
    assert messages[0].additional_kwargs[EPHEMERAL_BLOCKS_KEY] == [1, 3]

    _simulate_turns(compressor, messages, [_step_with_notices(4), _step_with_notices(5)])
    assert messages[0].content == [
        {"type": "text", "text": "Action 1 completed."},
        {"type": "text", "text": "Body note 1"},
    ]
    assert EPHEMERAL_BLOCKS_KEY not in messages[0].additional_kwargs
    assert 0 in compressor._frozen


def test_grace_swap_before_the_text_edge_remaps_ephemeral_indices(mock_context):
    """Pending summary inside the grace window with the text edge still ahead:
    the keep-image revisit changes nothing (indices untouched); the late swap
    removes the label and remaps; the text pass then deletes the right blocks."""
    from artemis.memory.transcript import EPHEMERAL_BLOCKS_KEY

    summarizer = VisualStepSummarizer(mock_context)
    summarizer._step_inputs["tc1"] = {"step_number": 1}  # pending
    messages: list = []
    compressor = ScrubEdgeCompressor(
        summarizer=summarizer, image_scrub_depth=3, xml_scrub_depth=5, pending_grace_steps=3
    )
    _simulate_turns(compressor, messages, [_step_with_notices(i) for i in range(1, 4)])
    # Depth 3, pending, in grace: byte-identical, indices untouched.
    assert _has_image(messages[0])
    assert "--- Current Screenshot ---" in str(messages[0].content)
    assert messages[0].additional_kwargs[EPHEMERAL_BLOCKS_KEY] == [1, 5]

    summarizer._summaries["tc1"] = "Late but in time."
    _simulate_turns(compressor, messages, [_step_with_notices(4)])  # depth 4: swap
    assert messages[0].content[2] == {
        "type": "text",
        "text": "--- Historical Visual Transition ---\nLate but in time.",
    }
    assert messages[0].additional_kwargs[EPHEMERAL_BLOCKS_KEY] == [1, 4]

    _simulate_turns(compressor, messages, [_step_with_notices(5), _step_with_notices(6)])
    # Depth 6 > 5: text pass on the remapped layout.
    assert messages[0].content == [
        {"type": "text", "text": "Action 1 completed."},
        {"type": "text", "text": "--- Historical Visual Transition ---\nLate but in time."},
        {"type": "text", "text": "Body note 1"},
    ]
    assert EPHEMERAL_BLOCKS_KEY not in messages[0].additional_kwargs
    assert 0 in compressor._frozen
