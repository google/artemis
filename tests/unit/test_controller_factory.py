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

from unittest.mock import MagicMock, patch

from artemis.context import ArtemisContext
from artemis.controllers.controller_factory import get_controller
from artemis.controllers.unified_controller import UnifiedMobileController


def test_get_controller_reuses_context_scoped_controller():
    ctx = MagicMock(spec=ArtemisContext)
    ctx._mobile_controller = None
    controller = object.__new__(UnifiedMobileController)

    with patch(
        "artemis.controllers.controller_factory.create_device_controller",
        return_value=controller,
    ) as create:
        assert get_controller(ctx) is controller
        assert get_controller(ctx) is controller

    create.assert_called_once_with(ctx)
