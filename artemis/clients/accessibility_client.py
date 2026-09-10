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

"""Artemis Accessibility Helper client for Android device automation.

Provides a lightweight, conflict-free alternative to UIAutomator2.
Runs concurrently with Mobly, Appium, and Espresso without monopolizing
the singleton UiAutomationConnection.
"""

from io import BytesIO
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from PIL import Image

from artemis.clients.ui_automator_client import UIAutomatorScreenData, _pil_to_base64
from artemis.runtime.adb_endpoint import adb_command
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

PACKAGE_NAME = "com.artemis.helper"
SERVICE_NAME = f"{PACKAGE_NAME}/.ArtemisAccessibilityService"
DEFAULT_PORT = 18888


class AccessibilityClient:
    """Non-exclusive accessibility client for Artemis UI automation."""

    def __init__(self, device_id: str, local_port: int = DEFAULT_PORT):
        self._device_id = device_id
        self._local_port = local_port
        self._base_url = f"http://127.0.0.1:{local_port}"

    def ensure_service_ready(self, apk_path: str | None = None) -> bool:
        """Ensure ArtemisAccessibilityHelper is installed, enabled, and port-forwarded."""
        # 1. Check if package is installed
        if not self._is_installed():
            if apk_path is None:
                default_apk = os.path.abspath(
                    os.path.join(
                        os.path.dirname(__file__),
                        "../../packages/artemis-accessibility-helper/ArtemisAccessibilityHelper.apk",
                    )
                )
                if os.path.exists(default_apk):
                    apk_path = default_apk

            if apk_path and os.path.exists(apk_path):
                logger.info(f"Installing ArtemisAccessibilityHelper on {self._device_id}...")
                self._run_adb(["install", "-r", "-g", apk_path])
            else:
                logger.warning(
                    f"ArtemisAccessibilityHelper not installed on {self._device_id} and no APK provided."
                )

        # 2. Ensure accessibility service is enabled via secure settings
        self._enable_accessibility_service()

        # 3. Setup port forward
        self._setup_port_forward()

        # 4. Verify connection
        for attempt in range(5):
            if self.ping():
                logger.info(f"ArtemisAccessibilityHelper connected successfully on {self._device_id}")
                return True
            time.sleep(0.5)

        logger.warning(f"Failed to connect to ArtemisAccessibilityHelper on port {self._local_port}")
        return False

    def ping(self) -> bool:
        """Health check probe."""
        try:
            req = urllib.request.Request(f"{self._base_url}/ping")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode())
                return data.get("success", False)
        except Exception:
            return False

    def get_screenshot(self) -> Image.Image | None:
        """Capture screenshot via high-speed ADB exec-out."""
        try:
            result = subprocess.run(
                adb_command(["-s", self._device_id, "exec-out", "screencap", "-p"]),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=True,
            )
            return Image.open(BytesIO(result.stdout))
        except Exception as e:
            logger.error(f"Failed to capture screenshot via adb: {e}")
            return None

    def get_hierarchy(self) -> dict:
        """Fetch hierarchy JSON directly from ArtemisAccessibilityHelper."""
        req = urllib.request.Request(f"{self._base_url}/dump")
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return json.loads(resp.read().decode())

    def get_hierarchy_xml(self) -> str:
        """Fetch raw standard UIAutomator XML string directly from helper."""
        req = urllib.request.Request(f"{self._base_url}/dump_xml")
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.read().decode("utf-8")

    def get_atomic_snapshot(self) -> dict | None:
        """Fetch atomic snapshot (screenshot Base64 + hierarchy) directly in one call."""
        try:
            req = urllib.request.Request(f"{self._base_url}/snapshot")
            with urllib.request.urlopen(req, timeout=6.0) as resp:
                data = json.loads(resp.read().decode())
                if data.get("success") and data.get("has_screenshot") and data.get("screenshot_base64"):
                    return data
        except Exception as e:
            logger.debug(f"Atomic snapshot request failed (falling back to dual path): {e}")
        return None

    def get_screen_data(self) -> UIAutomatorScreenData:
        """Retrieve complete screen data (screenshot + parsed elements + standard XML).

        Prioritizes the atomic hardware snapshot API (Android 11+) to eliminate
        any temporal phase mismatch between visual frame and layout DOM tree.
        Falls back to separate ADB screencap + HTTP dump on older platforms.
        """
        # 1. Try atomic snapshot
        atomic = self.get_atomic_snapshot()
        if atomic is not None:
            return UIAutomatorScreenData(
                base64=atomic.get("screenshot_base64", ""),
                hierarchy_xml=atomic.get("xml", ""),
                elements=atomic.get("elements", []),
                width=atomic.get("width", 1080),
                height=atomic.get("height", 2400),
            )

        # 2. Fallback: separate screenshot and hierarchy dump
        screenshot = self.get_screenshot()
        if screenshot is None:
            raise RuntimeError("Failed to capture screenshot")

        hierarchy_data = self.get_hierarchy()
        elements = hierarchy_data.get("elements", [])
        hierarchy_xml = hierarchy_data.get("xml", "")

        return UIAutomatorScreenData(
            base64=_pil_to_base64(screenshot, format="JPEG"),
            hierarchy_xml=hierarchy_xml,
            elements=elements,
            width=screenshot.width,
            height=screenshot.height,
        )

    def tap(self, x: float, y: float) -> bool:
        """Perform tap gesture via AccessibilityService."""
        return self._send_rpc("tap", {"x": x, "y": y})

    def swipe(self, x1: float, y1: float, x2: float, y2: float, duration_ms: int = 300) -> bool:
        """Perform swipe gesture via AccessibilityService."""
        return self._send_rpc(
            "swipe",
            {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration": duration_ms},
        )

    def send_text(self, text: str) -> bool:
        """Send text input via AccessibilityNodeInfo.ACTION_SET_TEXT."""
        success = self._send_rpc("type", {"text": text})
        if not success:
            # Fallback to adb shell input text
            safe_text = text.replace(" ", "%s")
            self._run_adb(["shell", "input", "text", safe_text])
            return True
        return success

    def clear_text(self) -> bool:
        """Clear text of focused input."""
        return self._send_rpc("clear", {})

    def press_key(self, key: str) -> bool:
        """Perform global navigation action (back, home, recents)."""
        key_lower = key.lower()
        if key_lower in ("back", "home", "recents", "notifications", "quick_settings"):
            return self._send_rpc("global", {"action": key_lower})
        # Fallback to adb keyevent
        self._run_adb(["shell", "input", "keyevent", f"KEYCODE_{key.upper()}"])
        return True

    def _send_rpc(self, cmd: str, params: dict) -> bool:
        """Send JSON-RPC payload to helper server."""
        payload = {"cmd": cmd, **params}
        try:
            req = urllib.request.Request(
                f"{self._base_url}/action",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read().decode())
                return data.get("success", False)
        except Exception as e:
            logger.warning(f"RPC command '{cmd}' failed: {e}")
            return False

    def _is_installed(self) -> bool:
        res = self._run_adb(["shell", "pm", "list", "packages", PACKAGE_NAME])
        return f"package:{PACKAGE_NAME}" in res.stdout

    def _enable_accessibility_service(self) -> None:
        """Silently enable the accessibility service via adb settings without UI prompts."""
        res = self._run_adb(["shell", "settings", "get", "secure", "enabled_accessibility_services"])
        current_services = res.stdout.strip()
        if SERVICE_NAME not in current_services:
            new_services = f"{current_services}:{SERVICE_NAME}" if current_services and current_services != "null" else SERVICE_NAME
            self._run_adb(["shell", "settings", "put", "secure", "enabled_accessibility_services", new_services])
        self._run_adb(["shell", "settings", "put", "secure", "accessibility_enabled", "1"])

    def _setup_port_forward(self) -> None:
        self._run_adb(["forward", f"tcp:{self._local_port}", f"tcp:{DEFAULT_PORT}"])

    def _run_adb(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            adb_command(["-s", self._device_id] + args),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
