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

"""Tests for ``strip_json_comments``.

The previous regex implementation treated ``//`` inside string literals as a
comment start, corrupting URL values like ``"api_base": "http://127.0.0.1:8080"``.
These tests pin comment removal to positions outside string literals.
"""

import io
import json

from third_party.mobile_use.utils.file import load_jsonc, strip_json_comments


def test_line_comment_removed_outside_string():
    text = '{\n  // a comment\n  "a": 1\n}'
    assert json.loads(strip_json_comments(text)) == {"a": 1}


def test_url_scheme_not_treated_as_comment():
    text = '{"api_base": "http://127.0.0.1:8080/v1"}'
    assert json.loads(strip_json_comments(text)) == {"api_base": "http://127.0.0.1:8080/v1"}


def test_https_and_trailing_comment_markers_in_string():
    text = '{"a": "https://x//y", "b": "keep // this"}'
    assert json.loads(strip_json_comments(text)) == {
        "a": "https://x//y",
        "b": "keep // this",
    }


def test_block_comment_removed_outside_string():
    text = '{\n  /* multi\n     line */\n  "a": 1\n}'
    assert json.loads(strip_json_comments(text)) == {"a": 1}


def test_block_comment_markers_inside_string_preserved():
    text = '{"a": "not a /* comment */", "b": 2}'
    assert json.loads(strip_json_comments(text)) == {
        "a": "not a /* comment */",
        "b": 2,
    }


def test_escaped_quote_does_not_end_string():
    # \" inside the string must not toggle string state, so the real "//"
    # comment after the closing quote is still removed.
    text = '{"a": "he said \\"http://x\\""} // real comment\n'
    assert json.loads(strip_json_comments(text)) == {"a": 'he said "http://x"'}


def test_escaped_backslash_before_quote():
    # After \\ the quote is not escaped, so the string ends there and the
    # tail is a comment.
    text = '{"a": "c:\\\\"} // tail\n'
    assert json.loads(strip_json_comments(text)) == {"a": "c:\\"}


def test_comment_only_line_leaves_newline_for_stable_line_numbers():
    text = '{\n// comment\n"a": 1\n}'
    stripped = strip_json_comments(text)
    assert stripped.count("\n") == 3


def test_unterminated_comment_consumes_to_eof():
    text = '{"a": 1} // never ends'
    assert json.loads(strip_json_comments(text)) == {"a": 1}


def test_load_jsonc_roundtrip(tmp_path):
    cfg = '{"llm": {"api_base": "http://127.0.0.1:8080/v1"}} // env'
    assert load_jsonc(io.StringIO(cfg)) == {"llm": {"api_base": "http://127.0.0.1:8080/v1"}}
