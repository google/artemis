"""Copy the license texts a redistributed bundle has to carry.

Run after Nuitka produces the dist directory:

    uv run python packaging/collect_licenses.py build/nuitka/artemis_cli.dist

Nuitka copies Python modules and package data, never the `*.dist-info`
directories that hold each dependency's license, so a bundle built without this
step redistributes the dependencies with no license text at all.
"""

from __future__ import annotations

import shutil
import sys
import sysconfig
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LICENSE_PATTERNS = ("LICENSE*", "LICENCE*", "COPYING*", "NOTICE*", "AUTHORS*")


def _license_files(metadata: Path) -> list[Path]:
    """Every license text a wheel ships, in the two layouts PEP 639 allows."""
    found: list[Path] = []
    for pattern in LICENSE_PATTERNS:
        found.extend(sorted(metadata.glob(pattern)))
    licenses_dir = metadata / "licenses"
    if licenses_dir.is_dir():
        found.extend(sorted(path for path in licenses_dir.rglob("*") if path.is_file()))
    return [path for path in found if path.is_file()]


def _dependency_licenses(site_packages: Path, target: Path) -> tuple[int, list[str]]:
    copied = 0
    undeclared: list[str] = []
    for metadata in sorted(site_packages.glob("*.dist-info")):
        package = metadata.name.split("-")[0]
        files = _license_files(metadata)
        if not files:
            undeclared.append(metadata.name.removesuffix(".dist-info"))
            continue
        for license_file in files:
            destination = target / f"{package}-{license_file.name}"
            if destination.exists():
                continue
            shutil.copyfile(license_file, destination)
            copied += 1
    return copied, undeclared


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: collect_licenses.py <dist directory>", file=sys.stderr)
        return 2

    dist = Path(argv[1]).resolve()
    if not dist.is_dir():
        print(f"not a directory: {dist}", file=sys.stderr)
        return 2

    licenses = dist / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)

    shutil.copyfile(REPO_ROOT / "LICENSE", dist / "LICENSE")
    shutil.copyfile(REPO_ROOT / "packaging" / "licenses" / "FFMPEG.txt", licenses / "FFMPEG.txt")
    for name in ("LICENSE", "NOTICE"):
        source = REPO_ROOT / "third_party" / "mobile_use" / name
        if source.is_file():
            shutil.copyfile(source, licenses / f"mobile_use-{name}")

    site_packages = Path(sysconfig.get_paths()["purelib"])
    copied, undeclared = _dependency_licenses(site_packages, licenses)
    if undeclared:
        summary = licenses / "THIRD_PARTY-SUMMARY.txt"
        summary.write_text(
            "Dependencies that declare no license file in their wheel metadata.\n"
            "Their license terms are recorded in each project's own repository.\n\n"
            + "\n".join(undeclared)
            + "\n",
            encoding="utf-8",
        )

    print(
        f"Licenses written to {licenses}: {copied} files from dependencies"
        f", {len(undeclared)} packages with no license file, plus Artemis and FFmpeg"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
