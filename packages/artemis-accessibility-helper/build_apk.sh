#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# 1. Locate Android SDK
SDK_ROOT="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}"
if [ ! -d "$SDK_ROOT" ]; then
    for candidate in /opt/android-sdk /usr/local/share/android-sdk "$HOME/Android/Sdk"; do
        if [ -d "$candidate" ]; then
            SDK_ROOT="$candidate"
            break
        fi
    done
fi

if [ ! -d "$SDK_ROOT" ]; then
    echo "Error: Android SDK not found at $SDK_ROOT. Set ANDROID_HOME." >&2
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
    if [ -d "/Applications/Android Studio.app/Contents/jbr/Contents/Home" ]; then
        export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
    fi
fi
if [ -n "$JAVA_HOME" ]; then
    export PATH="$JAVA_HOME/bin:$PATH"
fi

if ! command -v javac >/dev/null 2>&1; then
    echo "Error: javac not found in PATH or JAVA_HOME" >&2
    exit 1
fi

echo "=================================================="
echo " Building ArtemisAccessibilityHelper.apk"
echo " Build Tools: $(basename "$BUILD_TOOLS")"
echo " Platform:    $(basename "$PLATFORM")"
echo " Java:        $(javac -version 2>&1)"
echo "=================================================="

rm -rf build
mkdir -p build/gen build/obj build/apk build/res_compiled

echo "-> Compiling resources..."
"$BUILD_TOOLS/aapt2" compile --dir app/src/main/res -o build/res_compiled/res.zip

echo "-> Linking APK..."
"$BUILD_TOOLS/aapt2" link \
    -I "$PLATFORM/android.jar" \
    --manifest app/src/main/AndroidManifest.xml \
    --min-sdk-version 24 \
    --target-sdk-version 35 \
    --java build/gen \
    -o build/apk/unaligned.apk \
    build/res_compiled/res.zip

echo "-> Compiling Java sources..."
JAVA_FILES="$(find build/gen app/src/main/java -name "*.java")"
javac -encoding UTF-8 \
    -cp "$PLATFORM/android.jar" \
    -d build/obj \
    $JAVA_FILES

echo "-> Dexing with D8..."
CLASS_FILES="$(find build/obj -name "*.class")"
"$BUILD_TOOLS/d8" \
    --lib "$PLATFORM/android.jar" \
    --output build/apk/ \
    --min-api 24 \
    $CLASS_FILES

echo "-> Packaging DEX..."
cd build/apk
zip -u -q unaligned.apk classes.dex
cd "$DIR"

echo "-> Zipaligning..."
"$BUILD_TOOLS/zipalign" -f -p 4 build/apk/unaligned.apk build/apk/aligned.apk

echo "-> Signing APK..."
if [ ! -f debug.keystore ]; then
    echo "Creating persistent debug.keystore..."
    keytool -genkey -v -keystore debug.keystore \
        -storepass android -alias androiddebugkey -keypass android \
        -keyalg RSA -keysize 2048 -validity 10000 \
        -dname "CN=Artemis Debug,O=Artemis,C=US"
fi

"$BUILD_TOOLS/apksigner" sign \
    --ks debug.keystore \
    --ks-pass pass:android \
    --key-pass pass:android \
    --ks-key-alias androiddebugkey \
    --out ArtemisAccessibilityHelper.apk \
    build/apk/aligned.apk

echo "=================================================="
echo " Build SUCCESSFUL!"
echo " Artifact: $DIR/ArtemisAccessibilityHelper.apk"
ls -lh ArtemisAccessibilityHelper.apk
echo "=================================================="
