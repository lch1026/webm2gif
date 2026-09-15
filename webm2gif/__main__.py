"""Entry point: launches the GUI, or the CLI when ``--cli`` is passed."""

from __future__ import annotations

import sys
from typing import Optional, Sequence


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    cli_flags = {"--cli", "--check", "--help", "-h", "--version"}
    if any(argument in cli_flags for argument in arguments):
        from .cli import main as cli_main

        return cli_main(arguments)

    from .app import run

    inputs = [argument for argument in arguments if not argument.startswith("-")]
    return run(inputs)


if __name__ == "__main__":
    raise SystemExit(main())
