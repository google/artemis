#Requires -Version 5.1
# Builds the standalone Artemis bundle with Nuitka on Windows.
#
# A Windows bundle needs its own script because the Nuitka invocation, the
# binary name and the staging steps differ, while the include list must stay
# identical to build.sh so both bundles carry the same modules and data.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$OutputDir = if ($env:OUTPUT_DIR) { $env:OUTPUT_DIR } else { "build/nuitka" }
$Jobs = if ($env:JOBS) { $env:JOBS } else { "8" }
$Dist = Join-Path $Root (Join-Path $OutputDir "artemis_cli.dist")

# Nuitka reuses an existing dist directory, which leaves data files from an
# earlier build beside the new ones. The C build cache above it stays.
if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }

uv run python -m nuitka `
  --standalone `
  --assume-yes-for-downloads `
  "--jobs=$Jobs" `
  "--output-dir=$OutputDir" `
  --output-filename=artemis `
  --include-package=langchain `
  --include-package=langchain_core `
  --include-package=langchain_openai `
  --include-package=langchain_community `
  --include-package=langgraph `
  --include-package=mcp `
  --enable-plugin=no-qt `
  --nofollow-import-to=pytest `
  --nofollow-import-to=_pytest `
  --include-package=artemis `
  --include-package-data=artemis `
  --include-package=ipykernel `
  --include-package-data=ipykernel `
  --include-package=jupyter_client `
  --include-package-data=jupyter_client `
  --include-package-data=debugpy `
  --include-package-data=onnxruntime `
  --include-package-data=rapidocr `
  --include-package-data=cv2 `
  --include-package-data=adbutils `
  --include-package-data=uiautomator2 `
  --include-package=uvicorn `
  --include-package=apps.admin_console `
  --include-data-file=apps/admin_console/index.html=apps/admin_console/index.html `
  --include-data-file=config/artemis.jsonc=config/artemis.jsonc `
  --include-package-data=third_party `
  packaging/artemis_cli.py

if ($LASTEXITCODE -ne 0) { throw "Nuitka build failed with exit code $LASTEXITCODE" }

# The bundle runs on a machine with no browser session, so the Angular build is
# not shipped; the server answers with its own "not built" page.
Remove-Item -Recurse -Force (Join-Path $Dist "artemis/resources/showcase_ui") -ErrorAction SilentlyContinue

$HelperTarget = Join-Path $Dist "packages/artemis-accessibility-helper"
New-Item -ItemType Directory -Force -Path $HelperTarget | Out-Null
Copy-Item "packages/artemis-accessibility-helper/ArtemisAccessibilityHelper.apk" $HelperTarget
Copy-Item "packages/artemis-accessibility-helper/helper_manifest.json" $HelperTarget

uv run python packaging/collect_licenses.py $Dist

Write-Host "Bundle ready at $Dist (executable: artemis.exe at the dist root)"
