#!/usr/bin/env bash
# Standalone build for ArtemisAccessibilityHelper.apk (no Gradle daemon).
#
# Outputs, next to this script:
#   ArtemisAccessibilityHelper.apk   the signed, zipaligned artifact
#   helper_manifest.json             {version_code, version_name, sha256, built_at}
#
# The Python runtime (artemis/runtime/helper_manager.py) reads helper_manifest.json
# to decide whether a device needs an install or an upgrade, so the manifest must
# always be regenerated together with the APK. The version is sourced from
# app/build.gradle.kts (defaultConfig.versionCode / versionName) so the Gradle
# build and this script cannot drift.
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

_to_posix() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -u "$1"
    else
        printf '%s' "$1"
    fi
}

# 1. Locate Android SDK
SDK_ROOT="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
if [ -n "$SDK_ROOT" ]; then
    SDK_ROOT="$(_to_posix "$SDK_ROOT")"
fi
if [ -z "$SDK_ROOT" ] || [ ! -d "$SDK_ROOT" ]; then
    for candidate in \
        "$HOME/Library/Android/sdk" \
        "$HOME/Android/Sdk" \
        /opt/android-sdk \
        /usr/local/share/android-sdk \
        "$( [ -n "$LOCALAPPDATA" ] && _to_posix "$LOCALAPPDATA/Android/Sdk" )"; do
        if [ -n "$candidate" ] && [ -d "$candidate" ]; then
            SDK_ROOT="$candidate"
            break
        fi
    done
fi

if [ -z "$SDK_ROOT" ] || [ ! -d "$SDK_ROOT" ]; then
    echo "Error: Android SDK not found. Set ANDROID_HOME." >&2
    exit 1
fi

BUILD_TOOLS="$(find "$SDK_ROOT/build-tools" -maxdepth 1 -mindepth 1 | sort -V | tail -n 1)"
PLATFORM="$(find "$SDK_ROOT/platforms" -maxdepth 1 -mindepth 1 | sort -V | tail -n 1)"

if [ -z "$BUILD_TOOLS" ] || [ ! -d "$BUILD_TOOLS" ]; then
    echo "Error: No build-tools found under $SDK_ROOT/build-tools" >&2
    exit 1
fi

if [ -z "$PLATFORM" ] || [ ! -f "$PLATFORM/android.jar" ]; then
    echo "Error: android.jar not found under $SDK_ROOT/platforms" >&2
    exit 1
fi

# 2. Locate Java compiler and runtime
if [ -z "$JAVA_HOME" ]; then
    for candidate in \
        "/Applications/Android Studio.app/Contents/jbr/Contents/Home" \
        "$(_to_posix "${PROGRAMFILES:-/c/Program Files}/Android/Android Studio/jbr")" \
        "/opt/android-studio/jbr"; do
        if [ -n "$candidate" ] && [ -x "$candidate/bin/javac" ] || [ -x "$candidate/bin/javac.exe" ]; then
            export JAVA_HOME="$candidate"
            break
        fi
    done
fi
if [ -n "$JAVA_HOME" ]; then
    export PATH="$JAVA_HOME/bin:$PATH"
fi

if ! command -v javac >/dev/null 2>&1; then
    echo "Error: javac not found in PATH or JAVA_HOME" >&2
    exit 1
fi

PYTHON_BIN="${PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
    # The Windows Store "python" stub is on PATH but exits 49; prefer the project venv.
    for candidate in "$DIR/../../.venv/Scripts/python.exe" "$DIR/../../.venv/bin/python" python3 python; do
        if "$candidate" -c "import zipfile" >/dev/null 2>&1; then
            PYTHON_BIN="$candidate"
            break
        fi
    done
fi
if [ -z "$PYTHON_BIN" ]; then
    echo "Error: python3 not found (needed to package the DEX and write helper_manifest.json)" >&2
    exit 1
fi

# Build-tools ship as .exe / .bat wrappers on Windows; resolve the right name once.
_tool() {
    local base="$BUILD_TOOLS/$1"
    for suffix in "" ".exe" ".bat"; do
        if [ -f "$base$suffix" ]; then
            printf '%s' "$base$suffix"
            return 0
        fi
    done
    echo "Error: $1 not found under $BUILD_TOOLS" >&2
    exit 1
}
AAPT2="$(_tool aapt2)"
D8="$(_tool d8)"
ZIPALIGN="$(_tool zipalign)"
APKSIGNER="$(_tool apksigner)"

# 3. Version: single source is app/build.gradle.kts
VERSION_CODE="$(sed -n 's/^[[:space:]]*versionCode[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' app/build.gradle.kts | head -n 1)"
VERSION_NAME="$(sed -n 's/^[[:space:]]*versionName[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' app/build.gradle.kts | head -n 1)"
if [ -z "$VERSION_CODE" ] || [ -z "$VERSION_NAME" ]; then
    echo "Error: could not read versionCode / versionName from app/build.gradle.kts" >&2
    exit 1
fi

echo "=================================================="
echo " Building ArtemisAccessibilityHelper.apk"
echo " Version:     $VERSION_NAME (code $VERSION_CODE)"
echo " Build Tools: $(basename "$BUILD_TOOLS")"
echo " Platform:    $(basename "$PLATFORM")"
echo " Java:        $(javac -version 2>&1)"
echo "=================================================="

rm -rf build
mkdir -p build/gen build/obj build/apk build/res_compiled

echo "-> Compiling resources..."
"$AAPT2" compile --dir app/src/main/res -o build/res_compiled/res.zip

echo "-> Linking APK..."
"$AAPT2" link \
    -I "$PLATFORM/android.jar" \
    --manifest app/src/main/AndroidManifest.xml \
    --min-sdk-version 24 \
    --target-sdk-version 35 \
    --version-code "$VERSION_CODE" \
    --version-name "$VERSION_NAME" \
    --java build/gen \
    -o build/apk/unaligned.apk \
    build/res_compiled/res.zip

echo "-> Compiling Java sources..."
JAVA_FILES="$(find build/gen app/src/main/java -name "*.java")"
javac -encoding UTF-8 \
    -source 8 -target 8 \
    -Xlint:-options \
    -cp "$PLATFORM/android.jar" \
    -d build/obj \
    $JAVA_FILES

echo "-> Dexing with D8..."
CLASS_FILES="$(find build/obj -name "*.class")"
"$D8" \
    --lib "$PLATFORM/android.jar" \
    --output build/apk/ \
    --min-api 24 \
    $CLASS_FILES

echo "-> Packaging DEX..."
"$PYTHON_BIN" - <<'PY'
import zipfile
with zipfile.ZipFile("build/apk/unaligned.apk", "a", compression=zipfile.ZIP_DEFLATED) as apk:
    apk.write("build/apk/classes.dex", "classes.dex")
PY

echo "-> Zipaligning..."
"$ZIPALIGN" -f -p 4 build/apk/unaligned.apk build/apk/aligned.apk

echo "-> Signing APK..."
if [ ! -f debug.keystore ]; then
    echo "Creating persistent debug.keystore..."
    keytool -genkey -v -keystore debug.keystore \
        -storepass android -alias androiddebugkey -keypass android \
        -keyalg RSA -keysize 2048 -validity 10000 \
        -dname "CN=Artemis Debug,O=Artemis,C=US"
fi

"$APKSIGNER" sign \
    --ks debug.keystore \
    --ks-pass pass:android \
    --key-pass pass:android \
    --ks-key-alias androiddebugkey \
    --out ArtemisAccessibilityHelper.apk \
    build/apk/aligned.apk

echo "-> Writing helper_manifest.json..."
VERSION_CODE="$VERSION_CODE" VERSION_NAME="$VERSION_NAME" "$PYTHON_BIN" - <<'PY'
import datetime, hashlib, json, os
digest = hashlib.sha256(open("ArtemisAccessibilityHelper.apk", "rb").read()).hexdigest()
manifest = {
    "package": "com.artemis.helper",
    "version_code": int(os.environ["VERSION_CODE"]),
    "version_name": os.environ["VERSION_NAME"],
    "sha256": digest,
    "built_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
}
with open("helper_manifest.json", "w", encoding="utf-8", newline="\n") as fh:
    json.dump(manifest, fh, indent=2)
    fh.write("\n")
print(json.dumps(manifest, indent=2))
PY

echo "=================================================="
echo " Build SUCCESSFUL!"
echo " Artifact: $DIR/ArtemisAccessibilityHelper.apk"
ls -lh ArtemisAccessibilityHelper.apk
echo "=================================================="
