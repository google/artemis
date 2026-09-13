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

"""Safety-net failure taxonomy shared by the Validator's precondition gates.

The categories are produced by the XML gate (``precondition_xml``) and the
pixel gate (``precondition_pixel``) and consumed by the execution loop when it
opens an :mod:`artemis.agents.validator.incidents` record for the Operator.
"""

from enum import StrEnum


class ValidationErrorCategory(StrEnum):
    NONE = "none"
    TARGET_DISAPPEARED = "target_disappeared"
    TARGET_SHIFTED = "target_shifted"
    TARGET_OCCUPIED = "target_occupied"
    PIXEL_TARGET_DISAPPEARED = "pixel_target_disappeared"
    PIXEL_BYPASSED = "pixel_bypassed"
    XML_BYPASSED = "xml_bypassed"
    GENERAL = "general"
