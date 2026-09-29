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

"""Parse JSON/query-like boolean flags without treating non-empty strings as True."""

_FALSEY = frozenset({"0", "false", "no", "off", ""})
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def coerce_bool(value: object, default: bool = False) -> bool:
    """Coerce JSON, query, and form values to bool.

    ``bool("false")`` is True in Python because the string is non-empty.
    Clients that serialize flags as strings therefore must not go through
    ``bool()``.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    text = str(value).strip().lower()
    if text in _FALSEY:
        return False
    if text in _TRUTHY:
        return True
    return default
