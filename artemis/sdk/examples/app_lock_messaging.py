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

import asyncio

from artemis.sdk import Agent
from pydantic import BaseModel, Field


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


class MessageResult(BaseModel):
    """Structured result from messaging task."""

    model_config = {"ignored_types": (CyFunctionDetector,)}

    messages_sent: int = Field(..., description="Number of messages successfully sent")
    contacts: list[str] = Field(..., description="List of contacts messaged")
    success: bool = Field(..., description="Whether all messages were sent successfully")


async def main() -> None:
    # Create agent with default configuration
    agent = Agent()

    try:
        await agent.init()

        # Use app lock to keep execution in WhatsApp
        # This ensures the agent stays in the app and relaunches if needed
        task = (
            agent.new_task("Send 'Happy New Year!' message to Alice, Bob, and Charlie on WhatsApp")
            .with_name("send_new_year_messages")
            .with_locked_app_package("com.whatsapp")  # Lock to WhatsApp
            .with_output_format(MessageResult)
            .with_max_steps(600)  # Messaging tasks may need more steps
            .build()
        )

        print("Sending messages with app lock enabled...")
        print("The agent will stay in WhatsApp and relaunch if needed.\n")

        result = await agent.run_task(request=task)

        if result:
            print("\n=== Messaging Complete ===")
            print(f"Messages sent: {result.messages_sent}")
            print(f"Contacts: {', '.join(result.contacts)}")
            print(f"Success: {result.success}")
        else:
            print("Failed to send messages")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        await agent.clean()


if __name__ == "__main__":
    asyncio.run(main())
