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

"""Compatibility prompt builder backed by the canonical Operator templates."""

import json
from functools import lru_cache
from pathlib import Path

from jinja2 import Template

OPERATOR_HUMAN_TEMPLATE = """Goal: {{ goal }}
Active Sub-Goal: {{ sub_goal }}
Current Turn: {{ current_turn }}
Execution History:
{{ history }}
"""


@lru_cache(maxsize=1)
def load_operator_prompts() -> dict[str, str]:
    """Load the single canonical prompt source shared by all Operator entry points."""
    prompts_path = Path(__file__).with_name("operator.json")
    return json.loads(prompts_path.read_text(encoding="utf-8"))


class OperatorPromptBuilder:
    """Builds prompt messages and multimodal contents for the Operator Agent."""

    @classmethod
    def build_system_message(
        cls,
        template_name: str = "main_template",
        available_tools: frozenset[str] | None = None,
    ) -> str:
        from artemis.agents.operator.prompts import apply_operator_prompt_contract

        prompts = load_operator_prompts()
        try:
            return apply_operator_prompt_contract(
                prompts[template_name], available_tools=available_tools
            )
        except KeyError as exc:
            raise ValueError(f"Unknown Operator template: {template_name}") from exc

    @classmethod
    def build_human_message(
        cls,
        goal: str,
        sub_goal: str = "",
        current_turn: int = 1,
        history: str = "",
    ) -> str:
        template = Template(OPERATOR_HUMAN_TEMPLATE)
        return template.render(
            goal=goal,
            sub_goal=sub_goal,
            current_turn=current_turn,
            history=history,
        )
