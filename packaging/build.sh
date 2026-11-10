#!/usr/bin/env bash
# Builds the standalone Artemis bundle with Nuitka.
#
# Every runtime dependency is included, so the resulting directory runs on a
# machine with no Python and no virtualenv. The bundle needs about 2 GB of free
# disk while compiling.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OUTPUT_DIR="${OUTPUT_DIR:-build/nuitka}"

# Nuitka reuses an existing dist directory, which leaves data files from an
# earlier build beside the new ones. The C build cache above it stays.
rm -rf "$OUTPUT_DIR/artemis_cli.dist"

uv run python -m nuitka \
  --standalone \
  --assume-yes-for-downloads \
  --jobs=8 \
  --output-dir="$OUTPUT_DIR" \
  --output-filename=artemis-cli \
  --include-package=langchain \
  --include-package=langchain_core \
  --include-package=langchain_openai \
  --include-package=langchain_community \
  --include-package=langgraph \
  --include-package=mcp \
  --nofollow-import-to=pytest \
  --nofollow-import-to=_pytest \
  --include-package=artemis \
  --include-package-data=artemis \
  --include-package=ipykernel \
  --include-package-data=ipykernel \
  --include-package=jupyter_client \
  --include-package-data=debugpy \
  --include-package-data=jupyter_client \
  --include-package-data=onnxruntime \
  --include-package-data=rapidocr \
  --include-package-data=cv2 \
  --include-package-data=adbutils \
  --include-package-data=uiautomator2 \
  --include-package=uvicorn \
  --include-package=apps.admin_console \
  --include-data-file=apps/admin_console/index.html=apps/admin_console/index.html \
  --include-data-file=config/artemis.jsonc=config/artemis.jsonc \
  --include-package-data=third_party \
  packaging/artemis_cli.py

DIST="$ROOT/$OUTPUT_DIR/artemis_cli.dist"

rm -rf "$DIST/artemis/resources/showcase_ui"
mkdir -p "$DIST/packages/artemis-accessibility-helper"
cp packages/artemis-accessibility-helper/ArtemisAccessibilityHelper.apk \
   packages/artemis-accessibility-helper/helper_manifest.json \
   "$DIST/packages/artemis-accessibility-helper/"
uv run python packaging/collect_licenses.py "$DIST"
echo "Staged the accessibility helper APK into $DIST; frontend assets removed"
