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

"""Event Bus and Lifecycle Hooks System for ARTEMIS."""

from collections import defaultdict
from collections.abc import Callable
from enum import Enum
import inspect
import time
from typing import Any
from pydantic import BaseModel, Field
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class HookType(str, Enum):
    BEFORE_TASK = "before_task"
    AFTER_TASK = "after_task"
    BEFORE_STEP = "before_step"
    AFTER_STEP = "after_step"
    ON_ACTION = "on_action"
    ON_ERROR = "on_error"
    ON_SCREEN_CHANGE = "on_screen_change"


class Event(BaseModel):
    """Encapsulates an event payload emitted through the event bus."""

    event_type: HookType = Field(..., description="Type of the lifecycle event")
    timestamp: float = Field(default_factory=time.time, description="Event occurrence timestamp")
    trace_id: str = Field(..., description="Active session trace identifier")
    payload: dict[str, Any] = Field(default_factory=dict, description="Event data dictionary")


class EventBus:
    """Asynchronous event bus supporting subscription and broadcast of lifecycle events."""

    def __init__(self):
        self._subscribers: dict[HookType, list[Callable[..., Any]]] = defaultdict(list)

    def subscribe(self, hook: HookType, callback: Callable[..., Any]) -> None:
        """Registers a listener callback for a specific lifecycle event."""
        self._subscribers[hook].append(callback)

    def unsubscribe(self, hook: HookType, callback: Callable[..., Any]) -> None:
        """Removes a registered callback."""
        if callback in self._subscribers[hook]:
            self._subscribers[hook].remove(callback)

    async def emit(self, event: Event) -> None:
        """Asynchronously dispatches an event to all subscribed listeners."""
        listeners = self._subscribers.get(event.event_type, [])
        for listener in listeners:
            try:
                if inspect.iscoroutinefunction(listener):
                    await listener(event)
                else:
                    listener(event)
            except Exception as e:
                logger.warning(f"Error executing event listener for {event.event_type}: {e}")


# Global singleton event bus instance
global_event_bus = EventBus()
