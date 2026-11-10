# Building the standalone bundle

The bundle runs Artemis on a machine with no Python and no virtualenv. It is
built with Nuitka and lives at `build/nuitka/artemis_cli.dist` after a successful
run.

## Build

```bash
bash packaging/build.sh          # macOS
pwsh packaging/build.ps1         # Windows
```

`JOBS` sets how many C translation units Nuitka compiles at once, 8 by
default. Lower it on a machine that runs out of memory while compiling:

```bash
JOBS=4 bash packaging/build.sh
```

Both scripts pass the same Nuitka include list, and
`tests/unit/test_packaging_build_scripts.py` fails when they drift. Only the
platform-specific parts differ:

| | macOS | Windows |
| --- | --- | --- |
| what you run | `bin/artemis` | `bin\artemis.exe` |
| real executable | `artemis-app` at the dist root | `artemis.exe` at the dist root |
| how the names relate | symlink, because a file named `artemis` cannot sit beside the `artemis/` package-data directory on a case-insensitive filesystem, and moving the binary into `bin/` breaks dyld's `@executable_path`. dyld canonicalises a symlink, so the link gets the name and the libraries stay beside the real file | hardlink, which costs no extra disk |
| staging | `packaging/stage.py` | `packaging/stage.py` |

Requirements on the build machine:

- Build on the platform you ship: the bundled ffmpeg is the platform-specific
  binary `imageio_ffmpeg` provides, so a macOS build carries a macOS ffmpeg.
- Xcode command line tools, for clang.
- `uv`, which the script uses to resolve dependencies and to find the site
  packages directory.
- About 5 GB of free disk: 3.8 GB of generated C and object files, then a
  756 MB dist. A run that fills the disk stops with
  `OSError: [Errno 28] No space left on device` while generating the C sources.

Each script deletes both output directories before it starts. A dist kept from
an earlier layout leaves orphaned data files, and a build directory left by an
interrupted run keeps partial C files that trip a Nuitka assertion
(`AssertionError: .../module.PIL.c`). Compiled objects survive in ccache, so
the rebuild is still much faster than a cold one.

A cold build compiles 7555 C translation units.

Compilation is the only step each script performs itself. Everything after it
lives in `packaging/stage.py`, which both platforms run, so a staging change is
written once instead of twice:

```bash
uv run python packaging/stage.py build/nuitka/artemis_cli.dist
```

It removes the frontend, installs the helper APK, links the public entry,
collects licenses, writes `BUILD-INFO.txt`, and then checks the result. A
bundle that misses a helper file, an OCR model, the config, the license texts,
or a working `--version` fails the build instead of shipping. On an 8-core Mac at `JOBS=4` it
took 30 minutes. A rebuild after a Python source change reuses the generated C
where it can and takes 14 to 20 minutes.

Output:

```
build/nuitka/artemis_cli.dist/bin/artemis      the command to run (symlink; artemis.exe on Windows)
build/nuitka/artemis_cli.dist/BUILD-INFO.txt   commit, Python, Nuitka and platform of this artifact
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

`bin/artemis doctor` reports which of these it resolved and from where. On a
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

Credentials come from the environment first, then from dotenv files, and the
first file to define a key wins:

1. `.env` beside the executable. Only a compiled bundle reads it, which is what
   makes a bundle configurable by dropping one file next to it.
2. `.env` in the user application directory, or the repository root in a source
   checkout.
3. The user application directory again, skipped when it is the same file as 2.

A source checkout has no compiled executable, so it skips the first file and
reads the other two exactly as before.

## The daemon entry point

The executable runs the admin console server under `--daemon`, which is how the
Windows console starts Artemis:

```bash
./artemis-cli --daemon --port 8123
```

`--host` and `--port` are parsed by `apps/admin_console/server.py`, the same
function `artemis ui` calls, and default to `127.0.0.1:8000`. The bundle carries
no console script, so this flag is the only way to reach the server from the
compiled binary.

## The kernel entry point

The executable hosts its own Jupyter kernel. `--kernel -f <connection file>` is
handed to `ipykernel`, which is how the image processing agent runs generated
code without needing an external Python. Artemis writes a kernelspec pointing
at its own executable on first use, so nothing outside the bundle is involved.
