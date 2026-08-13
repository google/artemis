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

import json
from artemis.utils.verification import (
    append_verification_chat,
    get_verification_chat_path,
    get_verification_chat_rounds,
    read_verification_chat,
)


def test_get_verification_chat_path(tmp_path):
    subgoal_hash = "test_hash"
    expected_path = tmp_path / "notes" / f"verification_chat_{subgoal_hash}.json"
    path = get_verification_chat_path(tmp_path, subgoal_hash)
    assert path == expected_path


def test_read_verification_chat_not_exists(tmp_path):
    path = tmp_path / "non_existent.json"
    turns = read_verification_chat(path)
    assert turns == []


def test_read_verification_chat_exists(tmp_path):
    path = tmp_path / "chat.json"
    expected_turns = [{"role": "operator", "round": 1, "content": "hello"}]
    path.write_text(json.dumps(expected_turns), encoding="utf-8")
    turns = read_verification_chat(path)
    assert turns == expected_turns


def test_get_verification_chat_rounds():
    turns = [
        {"role": "operator", "round": 1, "content": "op1"},
        {"role": "checker", "round": 1, "content": "chk1"},
        {"role": "operator", "round": 2, "content": "op2"},
    ]
    max_op, max_chk = get_verification_chat_rounds(turns)
    assert max_op == 2
    assert max_chk == 1


def test_get_verification_chat_rounds_empty():
    max_op, max_chk = get_verification_chat_rounds([])
    assert max_op == 0
    assert max_chk == 0


def test_append_verification_chat(tmp_path):
    path = tmp_path / "chat.json"

    # Append to empty/non-existent
    success = append_verification_chat(path, "operator", "hello", 1)
    assert success
    assert path.exists()
    turns = json.loads(path.read_text(encoding="utf-8"))
    assert turns == [{"role": "operator", "round": 1, "content": "hello"}]

    # Append again
    success = append_verification_chat(path, "checker", "hi", 2)
    assert success
    turns = json.loads(path.read_text(encoding="utf-8"))
    assert len(turns) == 2
    assert turns[1] == {"role": "checker", "round": 2, "content": "hi"}
