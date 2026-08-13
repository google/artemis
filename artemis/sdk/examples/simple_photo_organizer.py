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

"""Simple Photo Organizer - Basic SDK Usage Example

This example demonstrates a straightforward way to use the Artemis SDK
without builders or advanced configuration. It performs a real-world automation
task:
1. Opens the photo gallery
2. Finds photos from a specific date
3. Creates an album and moves those photos into it

Run:
- python artemis/sdk/examples/simple_photo_organizer.py
"""

import asyncio
from datetime import date, timedelta
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


class PhotosResult(BaseModel):
    """Structured result from photo search."""

    model_config = {"ignored_types": (CyFunctionDetector,)}

    found_photos: int = Field(..., description="Number of photos found")
    date_range: str = Field(..., description="Date range of photos found")
    album_created: bool = Field(..., description="Whether an album was created")
    album_name: str = Field(..., description="Name of the created album")
    photos_moved: int = Field(0, description="Number of photos moved to the album")


async def main() -> None:
    # Create a simple agent with default configuration
    agent = Agent()

    try:
        # Initialize agent (finds a device, starts required servers)
        await agent.init()

        # Calculate yesterday's date for the example
        yesterday = date.today() - timedelta(days=1)
        formatted_date = yesterday.strftime("%B %d")  # e.g. "August 22"

        print(f"Looking for photos from {formatted_date}...")

        # First task: search for photos and organize them, with typed output
        result = await agent.run_task(
            goal=(
                "Open the Photos/Gallery app. Find photos taken on"
                f" {formatted_date}. Create a new album named '{formatted_date}"
                " Memories' and move those photos into it. Count how many"
                " photos were moved."
            ),
            output=PhotosResult,
            name="organize_photos",
        )

        # Handle and display the result
        if result:
            print("\n=== Photo Organization Complete ===")
            print(f"Found: {result.found_photos} photos from {result.date_range}")

            if result.album_created:
                print(f"Created album: '{result.album_name}'")
                print(f"Moved {result.photos_moved} photos to the album")
            else:
                print("No album was created")
        else:
            print("Failed to organize photos")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        # Always clean up resources
        await agent.clean()


if __name__ == "__main__":
    asyncio.run(main())
