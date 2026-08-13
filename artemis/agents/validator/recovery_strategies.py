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

"""Modular Recovery Strategies for the Validation and Self-Healing Engine."""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any
from artemis.drivers.base import BaseDeviceDriver
from artemis.utils.logger import get_logger
from pydantic import BaseModel, Field

logger = get_logger(__name__)


class RecoveryActionType(str, Enum):
    RETRY_DYNAMIC_LOCATOR = "retry_dynamic_locator"
    DISMISS_DIALOG = "dismiss_dialog"
    NAVIGATE_BACK = "navigate_back"
    SCROLL_AND_RETRY = "scroll_and_retry"
    RESTART_APP = "restart_app"
    FALLBACK_COORDINATES = "fallback_coordinates"


class RecoveryResult(BaseModel):
    """Result of executing an autonomous self-healing recovery strategy."""

    success: bool
    action_type: RecoveryActionType
    reason: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class BaseRecoveryStrategy(ABC):
    """Abstract interface for modular self-healing failure recovery tactics."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy identifier."""
        ...

    @abstractmethod
    async def can_handle(self, failure_context: dict[str, Any]) -> bool:
        """Determines if this strategy applies to the observed failure mode."""
        ...

    @abstractmethod
    async def execute(
        self,
        driver: BaseDeviceDriver,
        failure_context: dict[str, Any],
    ) -> RecoveryResult:
        """Executes the self-healing corrective actions."""
        ...


class DismissDialogRecoveryStrategy(BaseRecoveryStrategy):
    """Strategy to detect and dismiss unexpected modal dialogs, popups, or permission prompts."""

    @property
    def name(self) -> str:
        return "dismiss_dialog"

    async def can_handle(self, failure_context: dict[str, Any]) -> bool:
        category = failure_context.get("category", "")
        error_msg = failure_context.get("error_message", "").lower()
        return (
            category == "ui_obstruction"
            or "permission" in error_msg
            or "dialog" in error_msg
            or "popup" in error_msg
        )

    async def execute(
        self,
        driver: BaseDeviceDriver,
        failure_context: dict[str, Any],
    ) -> RecoveryResult:
        logger.info(
            "[Recovery] Attempting to dismiss unexpected dialog or overlay via Back button..."
        )
        success = await driver.press_key("back")
        return RecoveryResult(
            success=success,
            action_type=RecoveryActionType.DISMISS_DIALOG,
            reason="Sent BACK keyevent to dismiss modal overlay",
        )


class NavigateBackRecoveryStrategy(BaseRecoveryStrategy):
    """Strategy to navigate back when agent navigates to a wrong screen."""

    @property
    def name(self) -> str:
        return "navigate_back"

    async def can_handle(self, failure_context: dict[str, Any]) -> bool:
        category = failure_context.get("category", "")
        return category in ("wrong_screen", "dead_end", "navigation_error")

    async def execute(
        self,
        driver: BaseDeviceDriver,
        failure_context: dict[str, Any],
    ) -> RecoveryResult:
        logger.info("[Recovery] Wrong screen detected, rolling back navigation...")
        success = await driver.press_key("back")
        return RecoveryResult(
            success=success,
            action_type=RecoveryActionType.NAVIGATE_BACK,
            reason="Navigated back from incorrect screen",
        )


class ScrollAndSearchRecoveryStrategy(BaseRecoveryStrategy):
    """Strategy to scroll viewport when target element is off-screen."""

    @property
    def name(self) -> str:
        return "scroll_and_search"

    async def can_handle(self, failure_context: dict[str, Any]) -> bool:
        category = failure_context.get("category", "")
        return category in ("element_not_found", "off_screen", "target_missing")

    async def execute(
        self,
        driver: BaseDeviceDriver,
        failure_context: dict[str, Any],
    ) -> RecoveryResult:
        direction = failure_context.get("scroll_direction", "down")
        logger.info(f"[Recovery] Target missing, scrolling {direction} to reveal element...")
        success = await driver.swipe_direction(direction=direction)
        return RecoveryResult(
            success=success,
            action_type=RecoveryActionType.SCROLL_AND_RETRY,
            reason=f"Scrolled {direction} to discover offscreen element",
        )


class RecoveryStrategyRegistry:
    """Central registry and chain-of-responsibility for self-healing recovery strategies."""

    _strategies: list[BaseRecoveryStrategy] = [
        DismissDialogRecoveryStrategy(),
        NavigateBackRecoveryStrategy(),
        ScrollAndSearchRecoveryStrategy(),
    ]

    @classmethod
    def register(cls, strategy: BaseRecoveryStrategy) -> None:
        cls._strategies.append(strategy)

    @classmethod
    async def attempt_recovery(
        cls,
        driver: BaseDeviceDriver,
        failure_context: dict[str, Any],
    ) -> RecoveryResult | None:
        """Finds the first applicable strategy and executes recovery."""
        for strategy in cls._strategies:
            if await strategy.can_handle(failure_context):
                logger.info(f"Applying recovery strategy: {strategy.name}")
                return await strategy.execute(driver=driver, failure_context=failure_context)
        return None
