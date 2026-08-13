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

from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


def create_device_controller(ctx: ArtemisContext) -> UnifiedMobileController:
    """Factory creating the unified mobile controller delegating to device drivers."""
    return UnifiedMobileController(ctx)


def get_controller(ctx: ArtemisContext) -> UnifiedMobileController:
    """Backward-compatible alias for create_device_controller."""
    return create_device_controller(ctx)
