"""Single entry point for the compiled bundle.

``--kernel`` makes the same binary act as the Jupyter kernel host that the image
processing agent spawns, because a compiled executable cannot be started with
``-m ipykernel_launcher``.
"""

import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--kernel":
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from ipykernel.kernelapp import IPKernelApp

        IPKernelApp.launch_instance()
        return

    from artemis.interfaces.cli.main import cli

    cli()


if __name__ == "__main__":
    main()
