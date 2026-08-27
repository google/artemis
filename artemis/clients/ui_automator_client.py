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

"""UIAutomator2 client for Android device screen data retrieval.

Provides an alternative to the Maestro-based screen API with direct device
access.
Handles Maestro blocker detection and removal before connecting.
"""

import base64
from io import BytesIO
import re as _re
import subprocess
import time
from typing import TYPE_CHECKING
import xml.etree.ElementTree as ET

from PIL import Image
from pydantic import BaseModel
import uiautomator2 as u2

from artemis.runtime.awake_service import ensure_device_awake
from artemis.utils.logger import get_logger

if TYPE_CHECKING:
    from uiautomator2 import Device

logger = get_logger(__name__)


MAESTRO_PACKAGE = "dev.mobile.maestro"


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


class UIAutomatorScreenData(BaseModel):
    """Screen data response from UIAutomator2."""

    model_config = {"ignored_types": (CyFunctionDetector,)}

    base64: str
    hierarchy_xml: str
    elements: list[dict]
    width: int
    height: int


def _parse_hierarchy_xml_to_elements(hierarchy_xml: str) -> list[dict]:
    """Parse uiautomator2 XML hierarchy into a flat list of element dictionaries.

    The output format matches the existing screen API format with attributes
    like:
    - resource-id
    - text
    - content-desc
    - bounds (as string "[x1,y1][x2,y2]")
    - class
    - package
    - checkable, checked, clickable, enabled, focusable, focused
    - scrollable, long-clickable, password, selected

    Args:
        hierarchy_xml: XML string from uiautomator2.dump_hierarchy()

    Returns:
        Flat list of element dictionaries
    """
    elements: list[dict] = []

    try:
        root = ET.fromstring(hierarchy_xml)
    except ET.ParseError as e:
        logger.error(f"Failed to parse hierarchy XML: {e}")
        return elements

    def _extract_element(node: ET.Element) -> None:
        """Recursively extract elements from XML nodes."""
        element: dict = {}

        for attr_name, attr_value in node.attrib.items():
            if attr_name == "resource-id":
                element["resource-id"] = attr_value
            elif attr_name == "text":
                element["text"] = attr_value
            elif attr_name == "content-desc":
                element["content-desc"] = attr_value
                element["accessibilityText"] = attr_value
            elif attr_name == "bounds":
                element["bounds"] = attr_value

                match = _re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attr_value)
                if match:
                    x1, y1, x2, y2 = map(int, match.groups())
                    element["parsed_bounds"] = {
                        "left": x1,
                        "top": y1,
                        "right": x2,
                        "bottom": y2,
                    }
            elif attr_name == "class":
                element["class"] = attr_value
            elif attr_name == "package":
                element["package"] = attr_value
            elif attr_name in (
                "checkable",
                "checked",
                "clickable",
                "enabled",
                "focusable",
                "focused",
                "scrollable",
                "long-clickable",
                "password",
                "selected",
            ):
                element[attr_name] = attr_value
            else:
                element[attr_name] = attr_value

        if element:
            elements.append(element)

        for child in node:
            _extract_element(child)

    _extract_element(root)

    return elements


def _pil_to_base64(img: Image.Image, format: str = "JPEG", quality: int = 80) -> str:
    """Convert PIL Image to base64 string with high-quality compression."""
    buffer = BytesIO()
    if format.upper() in ("JPEG", "JPG"):
        format = "JPEG"
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.save(buffer, format=format, quality=quality, optimize=True)
    else:
        img.save(buffer, format=format)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def _is_package_installed(device_id: str, pkg: str) -> bool:
    """Check if a package is installed on the device."""
    try:
        result = subprocess.run(
            ["adb", "-s", device_id, "shell", "pm", "list", "packages"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning(f"Failed to list packages: {result.stderr}")
            return False

        lines = result.stdout.splitlines()
        target = f"package:{pkg}"
        return target in lines
    except subprocess.TimeoutExpired:
        logger.warning("Timeout checking installed packages")
        return False
    except Exception as e:
        logger.warning(f"Error checking installed packages: {e}")
        return False


def _uninstall_package(device_id: str, pkg: str) -> bool:
    """Uninstall a package from the device for the current user."""
    try:
        result = subprocess.run(
            [
                "adb",
                "-s",
                device_id,
                "shell",
                "pm",
                "uninstall",
                "--user",
                "0",
                pkg,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info(f"Successfully uninstalled {pkg}")
            return True
        else:
            logger.warning(f"Failed to uninstall {pkg}: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout uninstalling {pkg}")
        return False
    except Exception as e:
        logger.warning(f"Error uninstalling {pkg}: {e}")
        return False


def _ensure_maestro_not_installed(device_id: str) -> None:
    """Check if Maestro is installed and uninstall it.

    Maestro conflicts with uiautomator2 - if Maestro's package is installed,
    u2.connect() will fail. This function ensures Maestro is removed before
    attempting to connect.
    """
    if _is_package_installed(device_id, MAESTRO_PACKAGE):
        logger.warning(
            f"Maestro ({MAESTRO_PACKAGE}) detected - uninstalling to enable UIAutomator2..."
        )
        _uninstall_package(device_id, MAESTRO_PACKAGE)


class UIAutomatorClient:
    """UIAutomator2 client for Android screen data retrieval.

    This client uses uiautomator2 library for direct device communication,
    providing faster hierarchy and screenshot retrieval compared to Maestro.

    Important: Maestro must not be installed on the device as it conflicts
    with uiautomator2. This client automatically handles Maestro removal.
    """

    def __init__(self, device_id: str):
        """Initialize the UIAutomator client.

        Args:
            device_id: The Android device serial number (e.g., "emulator-5554")
        """
        self._device_id = device_id
        self._device: Device | None = None
        self._awake_strategy: str | None = None

    def _ensure_connected(self) -> "Device":
        """Ensure connection to the device, handling Maestro blocker.

        Returns:
            Connected uiautomator2 Device instance
        """
        if self._device is not None:
            try:
                # Quick check if connection is still alive
                self._device.info
                return self._device
            except Exception:
                logger.warning("UIAutomator2 connection lost, reconnecting...")
                self._device = None

        # Ensure Maestro is not blocking us
        _ensure_maestro_not_installed(self._device_id)

        # Enroll the device in the containing Artemis service lifetime before
        # UIAutomator2 reads the screen. Client disconnects do not release it.
        if self._awake_strategy is None:
            self._awake_strategy = ensure_device_awake(self._device_id)

        # Connect to device
        logger.info(f"Connecting UIAutomator2 to device: {self._device_id}")
        try:
            self._device = u2.connect(self._device_id)
        except Exception:
            self._awake_strategy = None
            raise
        logger.info("UIAutomator2 connected successfully")
        return self._device

    def connect(self) -> None:
        """Connect and activate this session's awake strategy without an action."""
        self._ensure_connected()

    def press_key(self, key: str):
        """Press a key on the device.

        Args:
            key: Key to press (e.g., "home", "back", "enter"...)
        """
        device = self._ensure_connected()
        return device.press(key=key)

    def send_text(self, text: str) -> None:
        """Send text input to the device using FastInputIME.

        This method supports special characters (e.g., 'ö') that ADB shell
        input text cannot handle. The original IME is restored after input.

        Args:
            text: The text to input
        """

        device = self._ensure_connected()
        device.set_fastinput_ime(True)
        # Allow IME to initialize
        time.sleep(0.3)
        try:
            device.send_keys(text)
            # Give FastInputIME time to process the broadcast and commit text
            # before switching it off and killing it.
            time.sleep(0.5)
        finally:
            device.set_fastinput_ime(False)

    def clear_text(self) -> bool:
        """Clear text of the currently focused element using UIAutomator2.

        Returns True on success, False on failure.
        """
        try:
            device = self._ensure_connected()
            device(focused=True).clear_text()
            return True
        except Exception as e:
            logger.error(f"UIAutomator2 clear_text failed: {e}")
            return False

    def set_clipboard(self, text: str) -> bool:
        """Set text to system clipboard via UIAutomator2.

        Args:
            text: The string to set in the device clipboard.

        Returns:
            True if successful, False otherwise.
        """
        try:
            device = self._ensure_connected()
            device.set_clipboard(text)
            return True
        except Exception as e:
            logger.debug(f"UIAutomator2 set_clipboard failed: {e}")
            return False

    def get_hierarchy(self) -> str:
        """Get the UI hierarchy XML from the device.

        Returns:
            Compressed UI hierarchy XML string
        """
        device = self._ensure_connected()
        return device.dump_hierarchy(compressed=True)

    def get_screenshot(self) -> Image.Image | None:
        """Capture a screenshot from the device.

        Uses adb screencap for more reliable capture of all layers (including
        popups), falling back to UIAutomator2 if it fails.

        Returns:
            PIL Image or None if capture failed
        """
        try:
            result = subprocess.run(
                ["adb", "-s", self._device_id, "exec-out", "screencap", "-p"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=True,
            )
            return Image.open(BytesIO(result.stdout))
        except Exception as e:
            logger.warning(
                f"Failed to capture screenshot via adb: {e}, falling back to uiautomator2"
            )
            device = self._ensure_connected()
            return device.screenshot()

    def get_screenshot_base64(self) -> str | None:
        """Capture a screenshot and return as base64 string.

        Returns:
            Base64 encoded PNG screenshot or None if capture failed
        """
        screenshot = self.get_screenshot()
        if screenshot is None:
            return None
        return _pil_to_base64(screenshot, format="JPEG")

    def get_screen_data(self) -> UIAutomatorScreenData:
        """Get complete screen data including screenshot and hierarchy.

        Returns:
            UIAutomatorScreenData with screenshot, hierarchy, elements, and
            dimensions
        """
        device = self._ensure_connected()

        # Get screenshot
        screenshot = self.get_screenshot()
        if screenshot is None:
            raise RuntimeError("Failed to capture screenshot")

        # Get hierarchy
        hierarchy_xml = device.dump_hierarchy(compressed=True)

        # Parse XML to flat elements list
        elements = _parse_hierarchy_xml_to_elements(hierarchy_xml)

        return UIAutomatorScreenData(
            base64=_pil_to_base64(screenshot, format="JPEG"),
            hierarchy_xml=hierarchy_xml,
            elements=elements,
            width=screenshot.width,
            height=screenshot.height,
        )

    def disconnect(self) -> None:
        """Disconnect this client without ending the host's awake lifetime."""
        self._awake_strategy = None
        self._device = None
        logger.info("UIAutomator2 client disconnected")


def get_client(device_id: str) -> UIAutomatorClient:
    """Factory function to create a UIAutomatorClient.

    Args:
        device_id: The Android device serial number

    Returns:
        UIAutomatorClient instance
    """
    return UIAutomatorClient(device_id=device_id)
