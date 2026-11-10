# Building the standalone bundle

The bundle runs Artemis on a machine with no Python and no virtualenv. It is
built with Nuitka and lives at `build/nuitka/artemis_cli.dist` after a successful
run.

## Build

```bash
bash packaging/build.sh          # macOS
pwsh packaging/build.ps1         # Windows
```

Both scripts pass the same Nuitka include list, and
`tests/unit/test_packaging_build_scripts.py` fails when they drift. Only the
platform-specific parts differ:

| | macOS | Windows |
| --- | --- | --- |
| executable | `artemis-cli` at the dist root | `artemis.exe` at the dist root |
| why the name differs | a bare `artemis` collides with the `artemis/` package-data directory on a case-insensitive filesystem | no collision with an `.exe` |
| staging | helper APK, then `collect_licenses.py` | same |

Requirements on the build machine:

- Build on the platform you ship: the bundled ffmpeg is the platform-specific
  binary `imageio_ffmpeg` provides, so a macOS build carries a macOS ffmpeg.
- Xcode command line tools, for clang.
- `uv`, which the script uses to resolve dependencies and to find the site
  packages directory.
- About 10 GB of free disk while compiling.

A cold build compiles roughly 7500 C translation units and takes 45 to 60
minutes. A rebuild after a Python source change reuses the generated C where it
can and takes 14 to 20 minutes.

Output:

```
build/nuitka/artemis_cli.dist/artemis-cli     the executable
build/nuitka/artemis_cli.dist/artemis/        package data: agent JSON and prompts
build/nuitka/artemis_cli.dist/config/         the shipped default configuration
build/nuitka/artemis_cli.dist/rapidocr/       ONNX models for local OCR
build/nuitka/artemis_cli.dist/imageio_ffmpeg/  ffmpeg for machines without one
build/nuitka/artemis_cli.dist/packages/       the accessibility helper APK
```

## What the bundle needs from the target machine

Three tools stay outside the bundle. Without the first one Artemis cannot reach
a device at all.

| Tool | Required | How it is found |
| --- | --- | --- |
| `adb` | yes | `ARTEMIS_ADB_PATH`, then `ANDROID_HOME` or `ANDROID_SDK_ROOT` under `platform-tools/adb`, then common install paths, then `PATH` |
| `scrcpy` | only for screen recording and live stream | `ARTEMIS_SCRCPY_PATH`, then common install paths, then `PATH` |
| `ffmpeg` | optional | The resolver checks `ARTEMIS_FFMPEG_PATH`, then common install paths, then `PATH`, and only then falls back to the copy `imageio_ffmpeg` ships, which the build includes under `imageio_ffmpeg/binaries` |

`artemis-cli doctor` reports which of these it resolved and from where. On a
machine that already has ffmpeg installed, the system copy is used and the
bundled one stays unused; it exists for machines that have none.

## No web frontend

The build drops the Angular bundle. The HTTP server still runs and serves the
whole API, which is what `run` enqueues into and what `doctor` reports on, but
`http://127.0.0.1:8000/` answers with the server's own "not built" page. `ui`
does not open a browser unless you pass `--open`.

## Licenses

The build stages the texts a redistribution has to carry. Nuitka copies modules
and package data, never the `*.dist-info` directories that hold each
dependency's license, so without this step the bundle ships the dependencies
with no licenses at all.

```
dist/LICENSE                 Artemis, Apache-2.0
dist/licenses/<pkg>-<file>   one file per license a dependency ships
dist/licenses/FFMPEG.txt     the bundled ffmpeg is a GPL build, with sources
dist/licenses/THIRD_PARTY-SUMMARY.txt   dependencies that ship no license file
```

`packaging/collect_licenses.py` performs that copy and runs as part of
`build.sh`. A Windows build must call it too.

The ffmpeg executable that `imageio_ffmpeg` provides is built with
`--enable-gpl`, so it is GPL-2.0-or-later, not LGPL. `FFMPEG.txt` records the
version, the build configuration, and where the corresponding source is
published. Artemis itself is Apache-2.0 and links none of it: ffmpeg runs as a
subprocess. A distribution that cannot accept GPL terms should delete the
executable from the bundle and require ffmpeg on the host, which `doctor`
already reports on.

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
