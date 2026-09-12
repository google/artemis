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

from io import StringIO
import json

import pytest

from artemis.utils.file import load_jsonc, strip_json_comments


@pytest.mark.parametrize(
    "value",
    [
        "https://gateway.example.test/v1",
        "https://gateway.example.test/*route*/models",
        'a quoted "value" with // and /* inside the string',
        'backslash \\ followed by a quote " and // literal',
    ],
)
def test_comments_do_not_corrupt_json_strings(value):
    document = (
        '// Leading comment with "quotes"\r\n'
        '{ /* block comment\n across lines */ "value": '
        + json.dumps(value)
        + ', // Inline comment\n "limit": 1 /* trailing comment */ }'
    )
    assert load_jsonc(StringIO(document)) == {"value": value, "limit": 1}
    stripped = strip_json_comments(document)
    assert len(stripped) == len(document)
    assert stripped.count("\n") == document.count("\n")


def test_comments_do_not_join_separate_number_tokens():
    with pytest.raises(json.JSONDecodeError):
        load_jsonc(StringIO('{"limit": 1/* not part of the number */2}'))
