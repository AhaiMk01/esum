"""The `esum` console script. A bare version flag is answered here, before the CLI and its HTTP
stack are imported (AXI §10): harnesses probe it often, so it must be quick."""

from __future__ import annotations

import sys

from . import __version__


def main() -> int:
    if sys.argv[1:] in (["-v"], ["-V"], ["--version"]):
        sys.stdout.write(f"{__version__}\n")
        return 0
    from .cli import main as cli_main
    return cli_main()
