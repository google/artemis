"""Single entry point for the compiled bundle.

``--kernel`` makes the same binary act as the Jupyter kernel host that the image
processing agent spawns, because a compiled executable cannot be started with
``-m ipykernel_launcher``.
"""

from pathlib import Path
import os
import sys


def _point_imageio_at_the_bundled_ffmpeg() -> None:
    """Use the ffmpeg staged beside this binary instead of the system one.

    ``imageio_ffmpeg.get_ffmpeg_exe`` honours this variable above everything
    else, so the copy staged by packaging/build.sh is found on a machine that
    has no ffmpeg installed.
    """
    bundled = Path(sys.executable).parent / "toolchain" / "ffmpeg"
    if bundled.is_file():
        os.environ.setdefault("IMAGEIO_FFMPEG_EXE", str(bundled))


def main() -> None:
    _point_imageio_at_the_bundled_ffmpeg()

    if len(sys.argv) > 1 and sys.argv[1] == "--admin":
        from apps.admin_console.server import main as admin_main

        admin_main(sys.argv[2:])
        return

    if len(sys.argv) > 1 and sys.argv[1] == "--kernel":
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from ipykernel.kernelapp import IPKernelApp

        IPKernelApp.launch_instance()
        return

    from artemis.interfaces.cli.main import cli

    cli()


if __name__ == "__main__":
    main()
