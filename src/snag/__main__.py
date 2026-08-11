"""Entry point: `snag` (or `python -m snag`)."""

from __future__ import annotations

import shutil
import sys


def main() -> int:
    try:
        from setproctitle import setproctitle
    except ImportError:  # optional: only affects how the process shows up in ps
        pass
    else:
        setproctitle("snag")

    if shutil.which("rsync") is None:
        print("snag needs rsync on PATH (and on the servers you connect to).", file=sys.stderr)
        return 1
    from .app import SnagApp

    SnagApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
