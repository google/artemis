"""The bundled helper APK, its manifest, and the Gradle version must agree.

``helper_manager`` decides installs and upgrades from ``helper_manifest.json``,
so a rebuilt APK without a regenerated manifest (or a version bump in Gradle
without a rebuild) would silently ship the wrong version gate.
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from artemis.runtime.helper_manager import (
    BUNDLED_APK_PATH,
    BUNDLED_MANIFEST_PATH,
    HELPER_PACKAGE_DIR,
    PACKAGE_NAME,
    load_bundled_helper,
)

pytestmark = pytest.mark.skipif(
    not BUNDLED_APK_PATH.is_file(), reason="bundled helper APK is not present in this checkout"
)


def test_manifest_matches_apk_and_gradle_version():
    manifest = json.loads(BUNDLED_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["package"] == PACKAGE_NAME
    assert manifest["sha256"] == hashlib.sha256(BUNDLED_APK_PATH.read_bytes()).hexdigest(), (
        "ArtemisAccessibilityHelper.apk was rebuilt without regenerating helper_manifest.json; "
        "run packages/artemis-accessibility-helper/build_apk.sh"
    )

    gradle = (HELPER_PACKAGE_DIR / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    version_code = int(re.search(r"versionCode\s*=\s*(\d+)", gradle).group(1))
    version_name = re.search(r'versionName\s*=\s*"([^"]+)"', gradle).group(1)
    assert manifest["version_code"] == version_code
    assert manifest["version_name"] == version_name


def test_load_bundled_helper_reads_manifest():
    bundled = load_bundled_helper()
    assert bundled is not None
    assert bundled.apk_path == BUNDLED_APK_PATH
    assert bundled.version_code >= 2  # first version that reports version_code on /ping


def test_load_bundled_helper_returns_none_when_missing(tmp_path):
    assert load_bundled_helper(tmp_path / "x.apk", tmp_path / "x.json") is None
