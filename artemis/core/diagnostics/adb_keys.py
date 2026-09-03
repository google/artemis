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

"""ADB Authentication Key Diagnostic and Automated Self-Healing Module.

Android 4.2.2+ requires RSA authentication keys (~/.android/adbkey) for USB debugging.
When key files become 0-bytes or corrupted (due to aborted writes, touch commands,
or disk sync issues), ADB fails to send the public key, causing connected devices
to silently remain in an 'unauthorized' state without ever displaying the authorization prompt.

This module provides detection and self-healing for corrupted ADB keys.
"""

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from artemis.toolchain import toolchain
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class AdbKeyStatus:
    """Status details for ADB authentication keys."""

    is_valid: bool
    is_corrupted: bool
    exists: bool
    key_path: str
    pub_key_path: str
    key_size_bytes: int = 0
    pub_key_size_bytes: int = 0
    error_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def get_adb_key_paths() -> tuple[Path, Path]:
    """Resolve the paths to the active ADB private and public key files."""
    adb_vendor_keys = os.environ.get("ADB_VENDOR_KEYS")
    if adb_vendor_keys:
        custom_priv = Path(adb_vendor_keys.split(os.pathsep)[0])
        custom_pub = Path(f"{custom_priv}.pub")
        return custom_priv, custom_pub

    android_dir = Path.home() / ".android"
    return android_dir / "adbkey", android_dir / "adbkey.pub"


def inspect_adb_keys() -> AdbKeyStatus:
    """Inspect ADB authentication RSA key files for corruption or invalid state."""
    priv_path, pub_path = get_adb_key_paths()

    if not priv_path.exists() and not pub_path.exists():
        # Fresh environment: ADB will automatically generate keys on first start
        return AdbKeyStatus(
            is_valid=True,
            is_corrupted=False,
            exists=False,
            key_path=str(priv_path),
            pub_key_path=str(pub_path),
            key_size_bytes=0,
            pub_key_size_bytes=0,
            error_reason=None,
        )

    priv_size = priv_path.stat().st_size if priv_path.exists() else 0
    pub_size = pub_path.stat().st_size if pub_path.exists() else 0

    # 1. Check for 0-byte or truncated private key file
    if priv_path.exists():
        if priv_size == 0:
            return AdbKeyStatus(
                is_valid=False,
                is_corrupted=True,
                exists=True,
                key_path=str(priv_path),
                pub_key_path=str(pub_path),
                key_size_bytes=0,
                pub_key_size_bytes=pub_size,
                error_reason="Private key file is 0 bytes (empty). ADB cannot authenticate devices.",
            )
        if priv_size < 100:
            return AdbKeyStatus(
                is_valid=False,
                is_corrupted=True,
                exists=True,
                key_path=str(priv_path),
                pub_key_path=str(pub_path),
                key_size_bytes=priv_size,
                pub_key_size_bytes=pub_size,
                error_reason=f"Private key file is suspiciously small ({priv_size} bytes).",
            )

        # Validate PEM header structure
        try:
            content = priv_path.read_text(encoding="utf-8", errors="replace")
            if "BEGIN" not in content or "PRIVATE KEY" not in content:
                return AdbKeyStatus(
                    is_valid=False,
                    is_corrupted=True,
                    exists=True,
                    key_path=str(priv_path),
                    pub_key_path=str(pub_path),
                    key_size_bytes=priv_size,
                    pub_key_size_bytes=pub_size,
                    error_reason="Private key does not contain a valid PEM private key header.",
                )
        except Exception as e:
            return AdbKeyStatus(
                is_valid=False,
                is_corrupted=True,
                exists=True,
                key_path=str(priv_path),
                pub_key_path=str(pub_path),
                key_size_bytes=priv_size,
                pub_key_size_bytes=pub_size,
                error_reason=f"Cannot read private key: {e}",
            )

    # 2. Check for 0-byte public key if it exists
    if pub_path.exists() and pub_size == 0:
        return AdbKeyStatus(
            is_valid=False,
            is_corrupted=True,
            exists=True,
            key_path=str(priv_path),
            pub_key_path=str(pub_path),
            key_size_bytes=priv_size,
            pub_key_size_bytes=0,
            error_reason="Public key file (adbkey.pub) is 0 bytes (empty).",
        )

    return AdbKeyStatus(
        is_valid=True,
        is_corrupted=False,
        exists=True,
        key_path=str(priv_path),
        pub_key_path=str(pub_path),
        key_size_bytes=priv_size,
        pub_key_size_bytes=pub_size,
        error_reason=None,
    )


def heal_adb_keys(adb_path: str | None = None, force: bool = False) -> dict[str, Any]:
    """Auto-heal corrupted ADB authentication keys.

    If keys are corrupted (or if `force=True`), this function:
    1. Backs up or removes corrupted key files.
    2. Restarts the ADB server to cleanly generate a fresh RSA 2048-bit key pair.
    3. Verifies that the new key pair is healthy and valid.
    """
    status = inspect_adb_keys()
    if not status.is_corrupted and not force:
        return {
            "success": True,
            "repaired": False,
            "message": "ADB keys are healthy, no repair required.",
            "status": status.to_dict(),
        }

    priv_path, pub_path = get_adb_key_paths()
    priv_path.parent.mkdir(parents=True, exist_ok=True)

    # Backup corrupted non-empty files before removal
    timestamp = int(time.time())
    if priv_path.exists() and priv_path.stat().st_size > 0:
        backup_path = priv_path.with_name(f"adbkey.corrupted.{timestamp}")
        try:
            shutil.copy2(priv_path, backup_path)
            logger.info(f"Backed up corrupted adbkey to {backup_path}")
        except Exception as e:
            logger.warning(f"Could not backup corrupted adbkey: {e}")

    # Remove corrupted key files
    try:
        if priv_path.exists():
            priv_path.unlink(missing_ok=True)
        if pub_path.exists():
            pub_path.unlink(missing_ok=True)
    except Exception as e:
        logger.error(f"Failed to remove corrupted adb keys: {e}")
        return {
            "success": False,
            "repaired": False,
            "message": f"Failed to remove corrupted ADB key files: {e}",
            "status": status.to_dict(),
        }

    # Resolve ADB executable
    resolved_adb = adb_path or toolchain.resolve("adb") or shutil.which("adb") or "adb"

    # Restart ADB server to trigger key generation
    try:
        subprocess.run(
            [resolved_adb, "kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        subprocess.run(
            [resolved_adb, "start-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        # Execute adb devices to trigger initial handshake & key material generation if needed
        subprocess.run(
            [resolved_adb, "devices"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception as e:
        logger.error(f"Error restarting ADB server during key healing: {e}")
        return {
            "success": False,
            "repaired": False,
            "message": f"Failed to restart ADB server: {e}",
            "status": status.to_dict(),
        }

    # Re-inspect to verify health
    new_status = inspect_adb_keys()
    if new_status.is_valid and new_status.key_size_bytes > 0:
        logger.info("Successfully healed corrupted ADB keys.")
        return {
            "success": True,
            "repaired": True,
            "message": "Corrupted ADB keys were successfully repaired and regenerated.",
            "status": new_status.to_dict(),
        }
    elif new_status.is_valid and not new_status.is_corrupted:
        return {
            "success": True,
            "repaired": True,
            "message": "Corrupted ADB keys cleared. Fresh keys will be created on first device connection.",
            "status": new_status.to_dict(),
        }
    else:
        return {
            "success": False,
            "repaired": False,
            "message": f"Key healing completed, but keys remain invalid: {new_status.error_reason}",
            "status": new_status.to_dict(),
        }
