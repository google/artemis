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

"""Constants and prompt templates for the UI Explorer agent."""

EXPLORE_DESCRIPTIONS = {
    "flash": {
        "description": (
            "[EXPLORER] Call this to ask the UI Explorer agent to "
            "locate coordinates on the screen layout by object detection."
        ),
        "query_description": (
            "Pipe-separated list of target element descriptions, semantic"
            " action terms, or color descriptors to search for concurrently"
            " (e.g., 'red settings button | gear icon')."
        ),
        "rule_prompt": (
            "Use the explorer tool to quickly check for visual icons, buttons, or text by name."
        ),
    },
    "pro": {
        "description": (
            "[EXPLORER] Call this to ask the UI Explorer agent to locate"
            " coordinates on the screen layout by executing XML, OCR,"
            " coordinate search, and object detection."
        ),
        "query_description": (
            "The target element or information to search for, including descriptions."
        ),
        "rule_prompt": (
            "Call the explorer tool to search, OCR, or inspect it (maximum {max_iterations} tries)."
        ),
        "version_prompt": (
            "You are running with a maximum of {max_iterations} turns: on your"
            " final turn, you will receive a warning reminding you to call"
            " submit_answer."
        ),
    },
    "ultra": {
        "description": (
            "[EXPLORER] Full visual reasoning agent. Deeply inspects screen"
            " regions and utilizes all perception/pixel-level tools with a high"
            " execution limit (up to {max_iterations} turns) for"
            " high-difficulty or layout-critical tasks."
        ),
        "query_description": (
            "The target element or information to search for, including descriptions."
        ),
        "rule_prompt": (
            "For highly complex visual searches, details validation, or layout"
            " verification, invoke the explorer tool to perform multi-turn"
            " visual reasoning and detailed coordinate checks (maximum"
            " {max_iterations} tries)."
        ),
        "version_prompt": (
            "You are running with a maximum of {max_iterations} turns: on your"
            " final turn, you will receive a warning reminding you to call"
            " submit_answer."
        ),
    },
}
