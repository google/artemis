# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Backward compatibility entrypoint for artemis CLI."""

import sys

import typer

from artemis.interfaces.cli.main import app

# Tests replace `app`; command discovery still needs the real Typer app.
from artemis.interfaces.cli.main import app as _typer_app


def _root_tokens() -> frozenset[str]:
    """Command names and root options registered on the Typer app."""
    group = typer.main.get_command(_typer_app)
    tokens: set[str] = {"-h"}
    # Typer may vendor its own click, so use getattr instead of isinstance.
    tokens.update(getattr(group, "commands", {}) or {})
    tokens.update(group.get_help_option_names(group.context_class(group)))
    for param in group.params:
        tokens.update(getattr(param, "opts", ()))
        tokens.update(getattr(param, "secondary_opts", ()))
    return frozenset(tokens)


def normalize_argv(argv: list[str]) -> list[str]:
    """Insert ``run`` when the first argument is not a known root command/flag.

    Keeps ``python -m artemis.main "some goal"`` working as ``run "some goal"``.
    """
    if len(argv) > 1 and argv[1] not in _root_tokens():
        return [argv[0], "run", *argv[1:]]
    return list(argv)


def cli():
    # If invoked directly as python -m artemis.main without subcommand 'run',
    # check if first argument is a goal rather than a subcommand
    sys.argv[:] = normalize_argv(sys.argv)
    app()


if __name__ == "__main__":
    cli()
