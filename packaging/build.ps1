#Requires -Version 5.1
# Builds the standalone Artemis bundle with Nuitka on Windows.
#
# A Windows bundle needs its own script because the Nuitka invocation and the
# binary name differ, while the include list must stay identical to build.sh so
# both bundles carry the same modules and data. Post-processing and the release
# checks live in packaging/stage.py, which both platforms share.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$OutputDir = if ($env:OUTPUT_DIR) { $env:OUTPUT_DIR } else { "build/nuitka" }
$Jobs = if ($env:JOBS) { $env:JOBS } else { "8" }
$Dist = Join-Path $Root (Join-Path $OutputDir "artemis_cli.dist")
$BuildDir = Join-Path $Root (Join-Path $OutputDir "artemis_cli.build")

# Nuitka reuses both directories. A dist kept from an earlier layout leaves
# orphaned data beside the new files, and a build directory from an interrupted
# run keeps partial C files that trip Nuitka's own assertion
# (`assert not os.path.isfile(filename)`).
if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }
if (Test-Path $BuildDir) { Remove-Item -Recurse -Force $BuildDir }

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

uv run python packaging/stage.py $Dist
if ($LASTEXITCODE -ne 0) { throw "Staging failed with exit code $LASTEXITCODE" }

Write-Host "Bundle ready at $Dist (entry: bin\artemis.exe)"
