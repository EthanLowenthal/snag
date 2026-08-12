"""Entry point: `snag` (or `python -m snag`)."""

from __future__ import annotations

import shutil
import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    from .cli import USAGE, CliError, parse

    if any(a in ("-h", "--help") for a in args):
        print(USAGE)
        return 0

    try:
        from setproctitle import setproctitle
    except ImportError:  # optional: only affects how the process shows up in ps
        pass
    else:
        setproctitle("snag")

    if shutil.which("rsync") is None:
        print("snag needs rsync on PATH (and on the servers you connect to).", file=sys.stderr)
        return 1

    try:
        start = parse(args)
    except CliError as exc:
        print(f"snag: {exc}", file=sys.stderr)
        print("try `snag --help`", file=sys.stderr)
        return 2

    from .app import SnagApp

    SnagApp(start).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
