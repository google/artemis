# Building the standalone bundle

The bundle runs Artemis on a machine with no Python and no virtualenv. It is
built with Nuitka and lives at `build/nuitka/launcher.dist` after a successful
run.

## Build

```bash
bash packaging/build.sh
```

Requirements on the build machine:

- macOS on Apple silicon. The staged ffmpeg is `imageio_ffmpeg`'s
  `ffmpeg-macos-aarch64` binary, so a Linux or Windows bundle needs a different
  one.
- Xcode command line tools, for clang.
- `uv`, which the script uses to resolve dependencies and to find the site
  packages directory.
- About 10 GB of free disk while compiling.

A cold build compiles roughly 7500 C translation units and takes 45 to 60
minutes. A rebuild after a Python source change reuses the generated C where it
can and takes 14 to 20 minutes.

Output:

```
build/nuitka/launcher.dist/artemis-cli     the executable
build/nuitka/launcher.dist/artemis/        package data: agent JSON, prompts, Showcase UI
build/nuitka/launcher.dist/config/         the shipped default configuration
build/nuitka/launcher.dist/rapidocr/       ONNX models for local OCR
build/nuitka/launcher.dist/toolchain/      ffmpeg staged for machines without one
build/nuitka/launcher.dist/packages/       the accessibility helper APK
```

## What the bundle needs from the target machine

Three tools stay outside the bundle. Without the first one Artemis cannot reach
a device at all.

| Tool | Required | How it is found |
| --- | --- | --- |
| `adb` | yes | `ARTEMIS_ADB_PATH`, then `ANDROID_HOME` or `ANDROID_SDK_ROOT` under `platform-tools/adb`, then common install paths, then `PATH` |
| `scrcpy` | only for screen recording and live stream | `ARTEMIS_SCRCPY_PATH`, then common install paths, then `PATH` |
| `ffmpeg` | optional | The resolver checks `ARTEMIS_FFMPEG_PATH`, then common install paths, then `PATH`, and only then falls back to `toolchain/ffmpeg` inside the bundle, which the launcher exposes through `IMAGEIO_FFMPEG_EXE` |

`artemis-cli doctor` reports which of these it resolved and from where. On a
machine that already has ffmpeg installed, the system copy is used and the
staged one stays unused; it exists for machines that have none.

## Runtime state

A bundled run keeps mutable state outside the bundle, under the per-user
application directory. On macOS that is
`~/Library/Application Support/artemis`, which holds `artemis.jsonc`, `traces/`
and session data. The API key file is read from the same directory, so a
configured machine keeps working when the bundle is replaced.

## Configuration precedence

The bundled `config/artemis.jsonc` is the default. A file in the user
application directory overrides it, and CLI flags override both.

## The kernel entry point

The executable hosts its own Jupyter kernel. `--kernel -f <connection file>` is
handed to `ipykernel`, which is how the image processing agent runs generated
code without needing an external Python. Artemis writes a kernelspec pointing
at its own executable on first use, so nothing outside the bundle is involved.
