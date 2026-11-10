"""Turn a Nuitka dist directory into a release bundle.

Both build scripts compile with Nuitka and hand the result to this module, so
every post-processing step exists once instead of once per shell. Run it by hand
to restage an existing directory:

    uv run python packaging/stage.py build/nuitka/artemis_cli.dist
"""

from __future__ import annotations

from datetime import UTC, datetime
import importlib.metadata
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_PACKAGE = "artemis-accessibility-helper"
HELPER_FILES = ("ArtemisAccessibilityHelper.apk", "helper_manifest.json")
MINIMUM_LICENSE_FILES = 100
SMOKE_TIMEOUT_SECONDS = 120
# A child that prints UTF-8 fails to decode under a GBK locale, and the reader
# thread swallows the error, so the output arrives truncated.
CHILD_ENCODING = "utf-8"


class StagingError(RuntimeError):
    """A staged bundle is missing something it has to ship."""


def real_executable_name() -> str:
    """Name of the compiled binary at the dist root.

    macOS case-insensitive filesystems forbid a file named ``artemis`` beside
    the ``artemis/`` package-data directory Nuitka writes, and Nuitka refuses to
    build that combination outright, so the binary carries a suffix there.
    """
    return "artemis.exe" if sys.platform == "win32" else "artemis-app"


def public_entry(dist: Path) -> Path:
    """The command a user runs.

    Only macOS needs the indirection. A Windows bundle started through a link
    resolves its modules from the link's directory and imports fail, because
    nothing canonicalises the link the way dyld does, so the binary stays where
    Nuitka put it.
    """
    if sys.platform == "win32":
        return dist / "artemis.exe"
    return dist / "bin" / "artemis"


def link_public_entry(dist: Path) -> Path:
    """Give the binary its public name.

    A symlink works on macOS because dyld canonicalises it and still resolves
    ``@executable_path`` beside the real file. On Windows the binary already has
    that name at the root, so there is nothing to create.
    """
    entry = public_entry(dist)
    if sys.platform == "win32":
        return entry
    entry.parent.mkdir(parents=True, exist_ok=True)
    if entry.exists() or entry.is_symlink():
        entry.unlink()
    entry.symlink_to(Path("..") / real_executable_name())
    return entry


def remove_unshipped_frontend(dist: Path) -> None:
    """Drop the Angular build, which the bundle answers for with its own page."""
    shutil.rmtree(dist / "artemis" / "resources" / "showcase_ui", ignore_errors=True)


def install_helper_package(dist: Path) -> None:
    """Ship the accessibility helper APK and its manifest beside the code."""
    target = dist / "packages" / HELPER_PACKAGE
    target.mkdir(parents=True, exist_ok=True)
    for name in HELPER_FILES:
        shutil.copy2(REPO_ROOT / "packages" / HELPER_PACKAGE / name, target / name)


def collect_licenses(dist: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import collect_licenses as collector
    finally:
        sys.path.pop(0)
    if collector.main(["collect_licenses.py", str(dist)]) != 0:
        raise StagingError("license collection failed")


def _git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding=CHILD_ENCODING,
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip() or "unknown"


def write_build_info(dist: Path) -> Path:
    """Record what produced this directory, which no other file states."""
    try:
        nuitka = importlib.metadata.version("nuitka")
    except importlib.metadata.PackageNotFoundError:
        nuitka = "unknown"
    lines = [
        f"artemis {importlib.metadata.version('artemis')}",
        f"commit: {_git_commit()}",
        f"python: {platform.python_version()}",
        f"nuitka: {nuitka}",
        f"platform: {platform.system().lower()} {platform.machine()}",
        f"executable: {real_executable_name()}",
        f"built_at: {datetime.now(UTC).isoformat(timespec='seconds')}",
    ]
    target = dist / "BUILD-INFO.txt"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _smoke_version(entry: Path) -> str | None:
    try:
        completed = subprocess.run(
            [str(entry), "--version"],
            capture_output=True,
            text=True,
            encoding=CHILD_ENCODING,
            errors="replace",
            timeout=SMOKE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"{entry} did not run: {error}"
    if completed.returncode != 0:
        return f"{entry} --version exited {completed.returncode}"
    return None


def verify(dist: Path, smoke: bool = True) -> list[str]:
    """Every check a release bundle has to pass, as a list of failures."""
    entry = public_entry(dist)
    failures: list[str] = []
    if not entry.exists():
        failures.append(f"missing public entry {entry}")
    if not (dist / real_executable_name()).exists():
        failures.append(f"missing binary {real_executable_name()}")
    if sys.platform != "win32" and not entry.is_symlink():
        failures.append(f"{entry} is not a symlink, so the binary name is exposed")
    if sys.platform == "win32" and (dist / "bin").exists():
        failures.append("bin/ exists on Windows, where a linked binary cannot import")

    helper = dist / "packages" / HELPER_PACKAGE
    for name in HELPER_FILES:
        if not (helper / name).exists():
            failures.append(f"missing helper file {name}")

    if not list((dist / "rapidocr" / "models").glob("*.onnx")):
        failures.append("no OCR models, so text recognition cannot work offline")

    if not (dist / "config" / "artemis.jsonc").exists():
        failures.append("missing config/artemis.jsonc")

    if (dist / "artemis" / "resources" / "showcase_ui").exists():
        failures.append("frontend still present, but the scripts remove it")

    licenses = dist / "licenses"
    shipped = [path for path in licenses.glob("*") if path.is_file()]
    if len(shipped) < MINIMUM_LICENSE_FILES:
        failures.append(
            f"only {len(shipped)} license files, expected at least {MINIMUM_LICENSE_FILES}"
        )
    for required in ("FFMPEG.txt",):
        if not (licenses / required).exists():
            failures.append(f"missing licenses/{required}")
    if not (dist / "LICENSE").exists():
        failures.append("missing LICENSE")

    if not (dist / "BUILD-INFO.txt").exists():
        failures.append("missing BUILD-INFO.txt")

    if smoke and entry.exists():
        problem = _smoke_version(entry)
        if problem:
            failures.append(problem)
    return failures


def stage(dist: Path, smoke: bool = True) -> list[str]:
    remove_unshipped_frontend(dist)
    install_helper_package(dist)
    entry = link_public_entry(dist)
    collect_licenses(dist)
    write_build_info(dist)
    failures = verify(dist, smoke=smoke)
    print(f"public entry: {entry}")
    print(f"build info:   {dist / 'BUILD-INFO.txt'}")
    return failures


def main(argv: list[str]) -> int:
    arguments = [item for item in argv[1:] if not item.startswith("--")]
    if not arguments:
        print("usage: stage.py <dist-directory> [--skip-smoke]", file=sys.stderr)
        return 2
    dist = Path(arguments[0]).resolve()
    if not dist.is_dir():
        print(f"not a directory: {dist}", file=sys.stderr)
        return 2
    failures = stage(dist, smoke="--skip-smoke" not in argv)
    if failures:
        print("\nStaging failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("Staging checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
