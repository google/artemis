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

"""Unit tests for the agent.memory configuration block (M1/M2)."""

from artemis.config import AgentGlobalConfig


def test_memory_defaults():
    cfg = AgentGlobalConfig.model_validate({})
    # 2 per the 2026-09-01 on-device baseline (serial queue at 1 pushed
    # summary-ready P90 past the scrub-edge grace window).
    assert cfg.memory.runtime.max_concurrency == 2
    assert cfg.memory.runtime.retry_limit == 3
    assert cfg.memory.runtime.flush_timeout_s == 30.0
    # M5 (2026-09-01): the transcript path ships ON by default; explicit
    # false is the byte-for-byte rollback switch (see the flip test below).
    assert cfg.memory.transcript.enabled is True
    assert cfg.memory.transcript.image_scrub_depth == 3
    assert cfg.memory.transcript.pending_grace_steps == 3
    assert cfg.memory.transcript.xml_scrub_depth == 1


def test_memory_transcript_flag_explicit_disable_is_rollback_switch():
    cfg = AgentGlobalConfig.model_validate(
        {"memory": {"transcript": {"enabled": False}}}
    )
    assert cfg.memory.transcript.enabled is False
    # Sibling scrub defaults are unaffected by flipping the flag.
    assert cfg.memory.transcript.image_scrub_depth == 3


def test_memory_explicit_values():
    cfg = AgentGlobalConfig.model_validate(
        {
            "memory": {
                "runtime": {"max_concurrency": 4, "retry_limit": 5, "flush_timeout_s": 10},
                "transcript": {
                    "image_scrub_depth": 5,
                    "pending_grace_steps": 1,
                    "xml_scrub_depth": 2,
                },
            }
        }
    )
    assert cfg.memory.runtime.max_concurrency == 4
    assert cfg.memory.runtime.retry_limit == 5
    assert cfg.memory.runtime.flush_timeout_s == 10.0
    assert cfg.memory.transcript.image_scrub_depth == 5
    assert cfg.memory.transcript.pending_grace_steps == 1
    assert cfg.memory.transcript.xml_scrub_depth == 2


def test_legacy_step_summarizer_retry_limit_seeds_memory_runtime():
    """An explicit legacy flash.step_summarizer.retry_limit keeps working."""
    cfg = AgentGlobalConfig.model_validate(
        {"flash": {"step_summarizer": {"retry_limit": 7}}}
    )
    assert cfg.flash.step_summarizer.retry_limit == 7
    assert cfg.memory.runtime.retry_limit == 7


def test_explicit_memory_runtime_wins_over_legacy_alias():
    cfg = AgentGlobalConfig.model_validate(
        {
            "flash": {"step_summarizer": {"retry_limit": 7}},
            "memory": {"runtime": {"retry_limit": 2}},
        }
    )
    assert cfg.memory.runtime.retry_limit == 2
    # The legacy key itself is untouched for back-compat readers.
    assert cfg.flash.step_summarizer.retry_limit == 7


def test_legacy_default_does_not_override_memory_default():
    """A step_summarizer block without retry_limit must not seed anything."""
    cfg = AgentGlobalConfig.model_validate(
        {"flash": {"step_summarizer": {"enabled": True}}}
    )
    assert cfg.memory.runtime.retry_limit == 3


def test_recall_and_similarity_defaults():
    """M4 defaults: recall block on, bounded; similarity hint on. The distance
    threshold was calibrated to 5 in M5 (2026-09-01 on-device dHash data:
    same screen <=4, different screens >=7, valley at 5-6)."""
    cfg = AgentGlobalConfig()
    assert cfg.memory.recall.enabled is True
    assert cfg.memory.recall.max_results == 5
    assert cfg.memory.recall.max_text_tokens == 2000
    assert cfg.memory.recall.max_image_steps == 1
    assert cfg.memory.transcript.similarity_hint is True
    assert cfg.memory.transcript.similarity_max_distance == 5
    assert cfg.memory.policies == {}


def test_chunking_era_cap_follows_max_chunks_by_default():
    """M5: max_eras is an independent key; None (default) follows max_chunks."""
    cfg = AgentGlobalConfig()
    assert cfg.memory.chunking.max_chunks == 8
    assert cfg.memory.chunking.max_eras is None

    overridden = AgentGlobalConfig.model_validate(
        {"memory": {"chunking": {"max_chunks": 12, "max_eras": 4}}}
    )
    assert overridden.memory.chunking.max_chunks == 12
    assert overridden.memory.chunking.max_eras == 4


def test_recall_and_policies_overridable():
    cfg = AgentGlobalConfig.model_validate(
        {
            "memory": {
                "recall": {"enabled": False, "max_results": 3},
                "transcript": {"similarity_hint": False},
                "policies": {"planner": {"last_n_detailed": 3}},
            }
        }
    )
    assert cfg.memory.recall.enabled is False
    assert cfg.memory.recall.max_results == 3
    assert cfg.memory.transcript.similarity_hint is False
    assert cfg.memory.policies == {"planner": {"last_n_detailed": 3}}
