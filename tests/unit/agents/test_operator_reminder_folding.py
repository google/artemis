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

"""Late reminders must not become a second system message."""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from artemis.agents.operator.operator import fold_reminder_into_system_prompt

REMINDER = "You have reached the maximum number of tool calls."


def _single_leading_system(messages: list) -> None:
    roles = [message.type for message in messages]
    assert roles[0] == "system"
    assert roles.count("system") == 1


def test_reminder_joins_string_system_content():
    messages = [SystemMessage(content="base prompt"), HumanMessage(content="go")]
    folded = fold_reminder_into_system_prompt(messages, REMINDER)

    _single_leading_system(folded)
    assert folded[0].content == f"base prompt\n\n{REMINDER}"
    assert len(folded) == len(messages)


def test_reminder_joins_block_system_content():
    messages = [
        SystemMessage(content=[{"type": "text", "text": "base prompt"}]),
        HumanMessage(content="go"),
    ]
    folded = fold_reminder_into_system_prompt(messages, REMINDER)

    _single_leading_system(folded)
    assert folded[0].content[-1] == {"type": "text", "text": REMINDER}


def test_reminder_becomes_leading_system_when_history_starts_with_a_user_turn():
    folded = fold_reminder_into_system_prompt([HumanMessage(content="go")], REMINDER)

    _single_leading_system(folded)
    assert folded[0].content == REMINDER


def test_input_list_is_not_mutated():
    messages = [SystemMessage(content="base prompt"), AIMessage(content="thinking")]
    fold_reminder_into_system_prompt(messages, REMINDER)

    assert messages[0].content == "base prompt"
    assert len(messages) == 2
