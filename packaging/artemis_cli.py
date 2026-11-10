"""Single entry point for the compiled bundle.

``--kernel`` makes the same binary act as the Jupyter kernel host that the image
processing agent spawns, because a compiled executable cannot be started with
``-m ipykernel_launcher``. ``--daemon`` makes it act as the admin console HTTP
server, because the bundle carries no console script for that. ``--task`` runs
one task worker, which the queue starts as its own process and Nuitka refuses
to launch with ``-m`` when the executable is the compiled binary.
"""

import sys


def main() -> None:
    command = sys.argv[1:2]
    rest = sys.argv[2:]

    if command == ["--kernel"]:
        sys.argv = [sys.argv[0], *rest]
        from ipykernel.kernelapp import IPKernelApp

        IPKernelApp.launch_instance()
        return

    if command == ["--task"]:
        from artemis.main import cli as task_cli

        sys.argv = [sys.argv[0], *rest]
        task_cli()
        return

    if command == ["--daemon"]:
        from apps.admin_console.server import main as run_admin_console

        run_admin_console(rest)
        return

    from artemis.interfaces.cli.main import cli

    cli()


if __name__ == "__main__":
    main()
