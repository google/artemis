#!/usr/bin/env python3
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

import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


def run_cmd(cmd):
    """Runs a local shell command and returns stdout, stderr."""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout.strip(), result.stderr.strip()


def adb_cmd(cmd):
    """Runs an adb command."""
    return run_cmd(f"adb {cmd}")


def dump_ui():
    """Dumps the UI hierarchy and returns it as an XML string."""
    # Try to dump to a specified path
    out, err = adb_cmd("shell uiautomator dump /data/local/tmp/uidump.xml")
    path = "/data/local/tmp/uidump.xml"

    if "dumped to" not in out and "dumped to" not in err:
        # Try default dump
        out, err = adb_cmd("shell uiautomator dump")
        match = re.search(r"dumped to: (.*)", out + "\n" + err)
        if match:
            path = match.group(1).strip()
        else:
            # Fallback common path
            path = "/sdcard/window_dump.xml"

    local_path = "temp_uidump.xml"
    # Ensure we remove local temp file if it exists
    if os.path.exists(local_path):
        os.remove(local_path)

    out, err = adb_cmd(f"pull {path} {local_path}")

    if os.path.exists(local_path):
        with open(local_path, encoding="utf-8") as f:
            content = f.read()
        os.remove(local_path)
        return content
    else:
        print(f"Failed to pull UI dump from {path}. Out: {out}, Err: {err}")
    return None


def parse_bounds(bounds_str):
    """Parses bounds string '[left,top][right,bottom]' into [left, top, right, bottom]."""
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
    if m:
        return [int(x) for x in m.groups()]
    return None


def get_center(bounds):
    """Returns the center (x, y) of the bounds."""
    return (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2


def find_nodes(xml_str, filter_fn):
    """Finds all nodes in the XML that match the filter function."""
    try:
        root = ET.fromstring(xml_str)
    except Exception as e:
        print(f"Failed to parse XML: {e}")
        return []

    matched = []
    for node in root.iter("node"):
        attrib = node.attrib
        if filter_fn(attrib):
            matched.append(attrib)
    return matched


def tap_element(element, description="element"):
    bounds_str = element.get("bounds")
    if bounds_str:
        bounds = parse_bounds(bounds_str)
        if bounds:
            x, y = get_center(bounds)
            print(f"Tapping {description} at [{x}, {y}]")
            adb_cmd(f"shell input tap {x} {y}")
            return True
    return False


def bypass_chrome_welcome(timeout=15):
    """Bypasses Chrome welcome/sync screens if they appear."""
    print("Checking for Chrome welcome/sync screens...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        xml = dump_ui()
        if not xml:
            time.sleep(1)
            continue

        # Check for "Accept & continue"
        accept_nodes = find_nodes(
            xml,
            lambda a: (
                a.get("text") == "Accept & continue" or "terms_accept" in a.get("resource-id", "")
            ),
        )
        if accept_nodes:
            print("Found 'Accept & continue' screen.")
            tap_element(accept_nodes[0], "'Accept & continue' button")
            time.sleep(2)
            continue

        # Check for "No thanks" or "Don't turn on" (Sync promo)
        no_thanks_nodes = find_nodes(
            xml,
            lambda a: (
                a.get("text") in ["No thanks", "Don't turn on"]
                or "negative_button" in a.get("resource-id", "")
            ),
        )
        if no_thanks_nodes:
            print("Found Sync/Promo screen.")
            tap_element(no_thanks_nodes[0], "'No thanks' button")
            time.sleep(2)
            continue

        # Check if we are already on a webpage or home page (e.g., search bar is visible, or youtube content)
        # If we don't see welcome screens anymore, we might be good.
        # Let's check if we see "Search or type URL" or similar Chrome home elements, or YouTube.
        youtube_nodes = find_nodes(
            xml,
            lambda a: (
                "youtube" in a.get("text", "").lower()
                or "youtube" in a.get("content-desc", "").lower()
            ),
        )
        chrome_home = find_nodes(
            xml,
            lambda a: "search or type web address" in a.get("text", "").lower(),
        )

        if youtube_nodes or chrome_home:
            print("Chrome seems to be in main browser view.")
            break

        print("Waiting for welcome screens to resolve or main view to appear...")
        time.sleep(1)


def play_first_video(timeout=20):
    """Finds and clicks the first video on the YouTube page."""
    print("Waiting for YouTube to load and finding a video...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        xml = dump_ui()
        if not xml:
            time.sleep(1)
            continue

        # Look for video elements.
        # Heuristic: clickable elements with content-desc containing "views" and "ago"
        # or elements that look like video titles.
        video_nodes = []

        # Try finding by content-desc (common for video cards)
        video_nodes = find_nodes(
            xml,
            lambda a: a.get("clickable") == "true" and "views" in a.get("content-desc", "").lower(),
        )

        # If not found, try finding any node with "views" and "ago" in text/desc and click it
        if not video_nodes:
            video_nodes = find_nodes(
                xml,
                lambda a: (
                    (
                        "views" in a.get("content-desc", "").lower()
                        and "ago" in a.get("content-desc", "").lower()
                    )
                    or ("views" in a.get("text", "").lower() and "ago" in a.get("text", "").lower())
                ),
            )

        # If still not found, maybe we are on a cookie consent page?
        # Check for "Before you continue to YouTube" or "Reject all" / "Accept all"
        consent_nodes = find_nodes(
            xml,
            lambda a: a.get("text") in ["Reject all", "Accept all", "I agree"],
        )
        if consent_nodes:
            print("Found cookie consent banner. Clicking 'Accept all' or 'I agree'...")
            # Prefer Accept/Agree
            agree_node = [n for n in consent_nodes if n.get("text") in ["Accept all", "I agree"]]
            node_to_click = agree_node[0] if agree_node else consent_nodes[0]
            tap_element(node_to_click, "Consent button")
            time.sleep(3)
            continue

        if video_nodes:
            print(f"Found {len(video_nodes)} potential video elements.")
            # Click the first one
            first_video = video_nodes[0]
            desc = first_video.get("content-desc", first_video.get("text", "video"))
            print(f"Selecting video: {desc[:50]}...")
            if tap_element(first_video, "first video"):
                return True

        print("Still waiting for videos to load...")
        time.sleep(2)

    print("Failed to find a video to play.")
    return False


def verify_playback(timeout=15):
    """Verifies that the video is playing."""
    print("Verifying playback...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        xml = dump_ui()
        if not xml:
            time.sleep(1)
            continue

        # Check for video player element
        player_nodes = find_nodes(
            xml,
            lambda a: (
                "video player" in a.get("text", "").lower()
                or "video player" in a.get("content-desc", "").lower()
            ),
        )
        if player_nodes:
            print("Video player detected on screen.")

            # Check if it's paused or if we need to click play
            # Sometimes there is a "Play video" button if it didn't auto-play
            play_button = find_nodes(
                xml,
                lambda a: a.get("text") == "Play" or a.get("content-desc") == "Play video",
            )
            if play_button:
                print("Video is paused. Clicking play...")
                tap_element(play_button[0], "Play button")
                time.sleep(2)
            else:
                print("Video seems to be playing (no play button blocking it, player active).")

            # Check for "TAP TO UNMUTE"
            unmute_button = find_nodes(xml, lambda a: "unmute" in a.get("text", "").lower())
            if unmute_button:
                print("Optional: Clicking unmute...")
                tap_element(unmute_button[0], "Unmute button")

            return True

        print("Waiting for video player to appear...")
        time.sleep(2)
    return False


def main():
    # Ensure a device is connected
    out, err = run_cmd("adb devices")
    devices = [
        line.split()[0]
        for line in out.splitlines()[1:]
        if line.strip() and not line.startswith("*")
    ]
    if not devices:
        print("No ADB devices found. Please connect a device and try again.")
        sys.exit(1)
    print(f"Using device: {devices[0]}")

    # Optional: Clear Chrome to test clean run (highly recommended for testing the script itself)
    # To enable, uncomment the line below:
    # print("Clearing Chrome data for a clean test...")
    # adb_cmd("shell pm clear com.android.chrome")
    # time.sleep(2)

    # Start Chrome and navigate to YouTube
    url = "https://m.youtube.com"
    print(f"Starting Chrome and navigating to {url}...")
    # -d specifies the data URI
    # -n specifies the component
    adb_cmd(f"shell am start -n com.android.chrome/com.google.android.apps.chrome.Main -d {url}")

    # Wait for app to launch
    time.sleep(3)

    # Step 1: Bypass welcome screens
    bypass_chrome_welcome()

    # Step 2: Play the first video
    if play_first_video():
        # Step 3: Verify it plays
        time.sleep(3)  # Wait for transition
        if verify_playback():
            print("SUCCESS: Chrome opened, navigated to YouTube, and video playback started!")
        else:
            print("FAILURE: Video player not detected or failed to play.")
    else:
        print("FAILURE: Could not find or click a video.")


if __name__ == "__main__":
    main()
