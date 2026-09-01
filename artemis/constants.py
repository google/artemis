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

RECURSION_LIMIT = 30000
MAX_MESSAGES_IN_HISTORY = 25

# Optimization Constants
CHECKER_MAX_ITERATIONS = 20
VALIDATOR_POLL_TIMEOUT = 2.0
VALIDATOR_POLL_INTERVAL = 0.2
VALIDATOR_OCR_INTERVAL = 1.0
VALIDATOR_UI_HIERARCHY_TIMEOUT = 1.0

from google.genai import types as _genai_types

SAFETY_SETTINGS_BLOCK_NONE = [
    _genai_types.SafetySetting(
        category=_genai_types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=_genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
    _genai_types.SafetySetting(
        category=_genai_types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=_genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
    _genai_types.SafetySetting(
        category=_genai_types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=_genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
    _genai_types.SafetySetting(
        category=_genai_types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=_genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
    _genai_types.SafetySetting(
        category=_genai_types.HarmCategory.HARM_CATEGORY_CIVIC_INTEGRITY,
        threshold=_genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
]
