"""Pieces the test modules share: scratch trees, a slowed transfer, and the two
primitives the UI tests drive Textual with.
"""

from __future__ import annotations

import os
from pathlib import Path

from textual.keys import _character_to_key

from snag.rsync import Transfer


class Throttled(Transfer):
    """A transfer slow enough that intermediate progress can be observed."""

    @property
    def command(self) -> list[str]:
        base = super().command
        return [base[0], "--bwlimit=2500", *base[1:]]


def make_tree(base: Path, big: int = 20_000_000) -> tuple[Path, Path]:
    """An empty local directory and a populated "remote" one, both under `base`."""
    local, remote = base / "local", base / "remote"
    (remote / "logs").mkdir(parents=True)
    local.mkdir(parents=True)
    (remote / "capture.raw").write_bytes(os.urandom(big))
    (remote / "summary.json").write_text('{"ok": true}\n')
    (remote / ".hidden").write_text("x\n")
    (remote / "logs" / "run.log").write_text("line\n" * 200)
    return local, remote


async def settle(pilot, predicate, tries: int = 400) -> bool:
    """Pump the UI until `predicate` holds. Real rsync and real listings take real time."""
    for _ in range(tries):
        await pilot.pause(0.05)
        if predicate():
            return True
    return False


async def write(pilot, text: str) -> None:
    """Type `text` a character at a time into whatever holds focus."""
    await pilot.press(*[_character_to_key(c) for c in text])
    await pilot.pause()
