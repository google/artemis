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

"""The optional judge nodes declared under ``nodes`` survive config expansion."""

from artemis.config.llm import LLMConfig, _expand_default_into_nodes, lightweight_judge_default

_DEFAULT = {
    "provider": "openai",
    "model": "gpt-main",
    "fallback": {"provider": "openai", "model": "gpt-fallback"},
}


def _parse(nodes: dict) -> LLMConfig:
    return LLMConfig.model_validate(
        _expand_default_into_nodes({"default": _DEFAULT, "nodes": nodes})
    )


def test_declared_judge_nodes_are_kept():
    cfg = _parse(
        {
            "validator_pixel_safety_net": {"model": "gpt-judge", "temperature": 0.0},
            "planner_validation": {"model": "gpt-judge", "temperature": 0.0},
        }
    )

    for node in ("validator_pixel_safety_net", "planner_validation"):
        judge = cfg.get_agent(node)
        assert judge.provider == "openai"
        assert judge.model == "gpt-judge"
        assert judge.temperature == 0.0
        # Unset keys come from the default, including the fallback.
        assert judge.fallback.model == "gpt-fallback"


def test_undeclared_judge_nodes_fall_back_to_the_lightweight_default():
    cfg = _parse({})

    assert cfg.validator_pixel_safety_net is None
    assert cfg.planner_validation is None
    assert cfg.get_agent("planner_validation") == lightweight_judge_default()
