#!/usr/bin/env bash
# Builds the standalone Artemis bundle with Nuitka.
#
# Every runtime dependency is included, so the resulting directory runs on a
# machine with no Python and no virtualenv. The bundle needs about 2 GB of free
# disk while compiling. Post-processing and the release checks live in
# packaging/stage.py, which both platforms share.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OUTPUT_DIR="${OUTPUT_DIR:-build/nuitka}"
JOBS="${JOBS:-8}"
DIST="$ROOT/$OUTPUT_DIR/artemis_cli.dist"

# Nuitka reuses both directories. A dist kept from an earlier layout leaves
# orphaned data beside the new files, and a build directory from an interrupted
# run keeps partial C files that trip Nuitka's own assertion
# (`assert not os.path.isfile(filename)`). Object files stay cached in ccache.
rm -rf "$DIST" "$ROOT/$OUTPUT_DIR/artemis_cli.build"

uv run python -m nuitka \
  --standalone \
  --assume-yes-for-downloads \
  --jobs="$JOBS" \
  --output-dir="$OUTPUT_DIR" \
  --output-filename=artemis-app \
  --include-package=langchain \
  --include-package=langchain_core \
  --include-package=langchain_openai \
  --include-package=langchain_community \
  --include-package=langgraph \
  --include-package=mcp \
  --enable-plugin=no-qt \
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

uv run python packaging/stage.py "$DIST"

echo "Bundle ready at $DIST (entry: bin/artemis)"
