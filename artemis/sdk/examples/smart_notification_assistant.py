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

"""Smart Notification Assistant - Intermediate SDK Usage Example

This example demonstrates more advanced SDK features including:
- TaskRequestBuilder pattern
- Multiple agent profiles for different reasoning tasks
- Tracing for debugging/visualization
- Structured output with Pydantic
- Exception handling

It performs a practical automation task:
1. Checks notification panel for unread notifications
2. Categorizes them by priority/app
3. Performs actions based on notification content

Run:
- python artemis/sdk/examples/smart_notification_assistant.py
"""

import asyncio
from datetime import datetime

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass


from artemis.config import LLM, LLMConfig, LLMConfigUtils, LLMWithFallback
from artemis.sdk import Agent
from artemis.sdk.builders import Builders
from artemis.sdk.types import AgentProfile
from artemis.sdk.types.exceptions import AgentError
from pydantic import BaseModel, Field


class NotificationPriority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class _CyFunctionDetectorMeta(type):
    def __instancecheck__(self, instance):
        name = type(instance).__name__
        return (
            name
            in (
                "cyfunction",
                "cython_function_or_method",
                "builtin_function_or_method",
            )
            or "cyfunction" in name.lower()
        )


class CyFunctionDetector(metaclass=_CyFunctionDetectorMeta):
    pass


class Notification(BaseModel):
    """Individual notification details."""

    model_config = {"ignored_types": (CyFunctionDetector,)}

    app_name: str = Field(..., description="Name of the app that sent the notification")
    title: str = Field(..., description="Title/header of the notification")
    message: str = Field(..., description="Message content of the notification")
    priority: NotificationPriority = Field(
        default=NotificationPriority.MEDIUM,
        description="Priority level of notification",
    )


class NotificationSummary(BaseModel):
    """Summary of all notifications."""

    model_config = {"ignored_types": (CyFunctionDetector,)}

    total_count: int = Field(..., description="Total number of notifications found")
    high_priority_count: int = Field(0, description="Count of high priority notifications")
    notifications: list[Notification] = Field(
        default_factory=list, description="List of individual notifications"
    )


def get_agent() -> Agent:
    # Create two specialized profiles:
    # 1. An analyzer profile for detailed inspection tasks
    analyzer_profile = AgentProfile(
        name="analyzer",
        llm_config=LLMConfig(
            planner=LLMWithFallback(
                provider="openrouter",
                model="meta-llama/llama-4-scout",
                fallback=LLM(provider="openrouter", model="meta-llama/llama-4-maverick"),
            ),
            utils=LLMConfigUtils(
                outputter=LLMWithFallback(
                    provider="openai",
                    model="gpt-5-nano",
                    fallback=LLM(provider="openai", model="gpt-5-mini"),
                ),
                hopper=LLMWithFallback(
                    provider="openai",
                    model="gpt-5-nano",
                    fallback=LLM(provider="openai", model="gpt-5-mini"),
                ),
            ),
        ),
        # from_file="/tmp/analyzer.jsonc"  # can be loaded from file
    )

    # 2. An action profile for handling easy & fast actions based on notifications
    action_profile = AgentProfile(
        name="note_taker",
        llm_config=LLMConfig(
            planner=LLMWithFallback(
                provider="openai",
                model="o3",
                fallback=LLM(provider="openai", model="gpt-5"),
            ),
            utils=LLMConfigUtils(
                outputter=LLMWithFallback(
                    provider="openai",
                    model="gpt-5-nano",
                    fallback=LLM(provider="openai", model="gpt-5-mini"),
                ),
                hopper=LLMWithFallback(
                    provider="openai",
                    model="gpt-5-nano",
                    fallback=LLM(provider="openai", model="gpt-5-mini"),
                ),
            ),
        ),
    )

    # Configure default task settings with tracing
    task_defaults = Builders.TaskDefaults.with_max_steps(200).build()

    # Configure the agent
    config = (
        Builders.AgentConfig.add_profiles(profiles=[analyzer_profile, action_profile])
        .with_default_profile(profile=action_profile)
        .with_default_task_config(config=task_defaults)
        .build()
    )
    return Agent(config=config)


async def main():
    # Set up traces directory with timestamp for uniqueness
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    traces_dir = f"/tmp/notification_traces/{timestamp}"
    agent = get_agent()

    try:
        # Initialize agent (finds a device, starts required servers)
        await agent.init()

        print("Checking for notifications...")

        # Task 1: Get and analyze notifications with analyzer profile
        notification_task = (
            agent.new_task(
                goal=(
                    "Open the notification panel (swipe down from top). Scroll"
                    " through the first 3 unread notifications. For each"
                    " notification, identify the app name, title, and content."
                    " Tag messages from messaging apps or email as high"
                    " priority."
                )
            )
            .with_output_format(NotificationSummary)
            .using_profile("analyzer")
            .with_name("notification_scan")
            .with_max_steps(400)
            .with_trace_recording(enabled=True, path=traces_dir)
            .build()
        )

        # Execute the task with proper exception handling
        try:
            notifications = await agent.run_task(request=notification_task)

            # Display the structured results
            if notifications:
                print("\n=== Notification Summary ===")
                print(f"Total notifications: {notifications.total_count}")
                print(f"High priority: {notifications.high_priority_count}")

                # Task 2: Create a note to store the notification summary
                response = await agent.run_task(
                    goal=(
                        "Open my Notes app and create a new note summarizing"
                        f" the following information:\n{notifications}"
                    ),
                    name="email_action",
                    profile="note_taker",
                )
                print(f"Action result: {response}")

            else:
                print("Failed to retrieve notifications")

        except AgentError as e:
            print(f"Agent error occurred: {e}")
        except Exception as e:
            print(f"Unexpected error: {type(e).__name__}: {e}")
            raise

    finally:
        # Clean up
        await agent.clean()
        print(f"\nTraces saved to: {traces_dir}")


if __name__ == "__main__":
    asyncio.run(main())
