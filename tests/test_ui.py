"""The UI end to end: connect, browse, mark, copy, cancel, resume."""

from __future__ import annotations

import os

import snag.app as app_mod
from snag import config as cfg_mod
from snag.rsync import Transfer

from helpers import Throttled, settle


def names(pane) -> list[str]:
    """The pane's entries, without the leading `..` row."""
    return [e.name for e in pane.rows[1:]]


async def test_browse_and_transfer(remote_server):
    local, remote = remote_server("fake-box", big=8_000_000)
    app = app_mod.SnagApp()
    notes: list[tuple[str, str | None]] = []

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        assert app.screen.query_one("#servers").row_count >= 1, "server list is empty"

        await pilot.press("enter")
        connected = await settle(
            pilot,
            lambda: isinstance(app.screen, app_mod.BrowserScreen)
            and bool(app.screen.remote_pane.entries),
        )
        assert connected, "never connected and listed the remote"
        screen = app.screen
        app.notify = lambda msg, **kw: notes.append((str(msg), kw.get("severity")))

        assert screen.remote_pane.path == str(remote), screen.remote_pane.path
        assert ".hidden" not in names(screen.remote_pane), "dotfiles shown by default"

        await pilot.press("tab")
        await pilot.pause()
        assert screen.focused_pane is screen.remote_pane, "tab did not move focus"

        await pilot.press("full_stop")
        await pilot.pause()
        assert ".hidden" in names(screen.remote_pane), "'.' did not reveal dotfiles"
        await pilot.press("full_stop")
        await pilot.pause()

        remote_pane = screen.remote_pane
        remote_pane.table.move_cursor(row=names(remote_pane).index("logs") + 1)
        await pilot.press("enter")
        await settle(pilot, lambda: remote_pane.path.endswith("logs"))
        assert names(remote_pane) == ["run.log"], f"in {remote_pane.path}"
        await pilot.press("backspace")
        await settle(pilot, lambda: remote_pane.path == str(remote))
        assert remote_pane.current() is not None and remote_pane.current().name == "logs", (
            "cursor was not returned to the exited directory"
        )

        remote_pane.table.move_cursor(row=names(remote_pane).index("capture.raw") + 1)
        await pilot.press("space")
        remote_pane.table.move_cursor(row=names(remote_pane).index("summary.json") + 1)
        await pilot.press("space")
        await pilot.pause()
        assert remote_pane.marked == {"capture.raw", "summary.json"}, str(remote_pane.marked)

        await pilot.press("c")
        await pilot.pause()
        row = next(iter(screen.query(app_mod.TransferRow)), None)
        assert row is not None, "no transfer row appeared"
        assert row.label_text == "← 2 items", row.label_text
        await settle(pilot, lambda: not row.active)
        assert not row.active, "transfer never finished"
        assert row.query_one(app_mod.ProgressBar).progress == 100, "bar did not end full"

        await settle(pilot, lambda: "capture.raw" in names(screen.local_pane))
        assert "capture.raw" in names(screen.local_pane), "destination pane never refreshed"
        assert (local / "capture.raw").stat().st_size == 8_000_000
        assert not (local / "logs").exists(), "an unmarked entry was copied"

        notes.clear()
        await pilot.press("tab")  # back to the local pane, which has no marks
        screen.local_pane.table.move_cursor(row=0)  # the ".." row
        await pilot.press("c")
        await pilot.pause()
        assert any("Nothing selected" in n[0] for n in notes), str(notes)
        started = list(screen.query(app_mod.TransferRow))
        assert len(started) == 1, f"{len(started)} transfer rows"

        notes.clear()
        await pilot.press("x")
        await pilot.pause()
        assert any("No transfer" in n[0] for n in notes), str(notes)

        await pilot.press("X")
        await pilot.pause()
        panel = screen.query_one(app_mod.TransferPanel)
        assert not list(panel.query(app_mod.TransferRow)), "finished rows were not cleared"
        assert panel.display is False, "empty transfer panel stayed visible"

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, app_mod.ServerScreen), "escape left the browser open"

    state = cfg_mod.STATE_FILE.read_text()
    assert "fake-box" in state and str(local) in state, state


async def test_cancel_and_resume(remote_server, monkeypatch):
    size = 24_000_000
    local, remote = remote_server("c-box", big=size)
    monkeypatch.setattr(app_mod, "Transfer", Throttled)
    app = app_mod.SnagApp()

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await settle(
            pilot,
            lambda: isinstance(app.screen, app_mod.BrowserScreen)
            and bool(app.screen.remote_pane.entries),
        )
        screen = app.screen
        await pilot.press("tab")
        rows = names(screen.remote_pane)
        screen.remote_pane.table.move_cursor(row=rows.index("capture.raw") + 1)
        await pilot.press("c")

        row = None

        def started() -> bool:
            nonlocal row
            row = next(iter(screen.query(app_mod.TransferRow)), None)
            return row is not None and (row.query_one(app_mod.ProgressBar).progress or 0) > 15

        assert await settle(pilot, started, tries=600), "transfer never got underway"
        await pilot.press("x")
        assert await settle(pilot, lambda: not row.active, tries=600), "cancel did not stop it"

        partial = local / "capture.raw"
        landed = partial.stat().st_size if partial.exists() else 0
        assert 0 < landed < size, f"{landed:,} bytes kept, wanted a partial file"

        monkeypatch.setattr(app_mod, "Transfer", Transfer)  # full speed for the resume
        screen.remote_pane.table.move_cursor(row=rows.index("capture.raw") + 1)
        await pilot.press("c")
        second = None

        def finished() -> bool:
            nonlocal second
            all_rows = list(screen.query(app_mod.TransferRow))
            second = all_rows[-1] if len(all_rows) > 1 else None
            return second is not None and not second.active

        assert await settle(pilot, finished, tries=600), "resumed transfer never finished"
        assert partial.stat().st_size == size, f"{partial.stat().st_size:,} of {size:,}"
        assert partial.read_bytes() == (remote / "capture.raw").read_bytes(), "content differs"


async def test_sort_modes_cycle(remote_server):
    """`s` walks the modes, `S` flips direction, and the cursor stays where it was."""
    _, remote = remote_server("s-box", big=8_000_000)
    (remote / "aaa.log").write_bytes(b"x" * 5_000)
    # Distinct sizes, extensions, and mtimes, so every mode gives a different order.
    for name, when in (("capture.raw", 1000), ("summary.json", 2000), ("aaa.log", 3000)):
        os.utime(remote / name, (when, when))

    app = app_mod.SnagApp()
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await settle(
            pilot,
            lambda: isinstance(app.screen, app_mod.BrowserScreen)
            and bool(app.screen.remote_pane.entries),
        )
        pane = app.screen.remote_pane
        await pilot.press("tab")
        await pilot.pause()
        assert app.screen.focused_pane is pane, "tab did not move focus to the remote"

        expected = {
            "name": ["logs", "aaa.log", "capture.raw", "summary.json"],
            "size": ["logs", "capture.raw", "aaa.log", "summary.json"],
            "kind": ["logs", "summary.json", "aaa.log", "capture.raw"],
            "modified": ["logs", "aaa.log", "summary.json", "capture.raw"],
        }
        assert pane.sort_mode == "name", "panes should open sorted by name"
        for mode in ("name", "size", "kind", "modified", "name"):
            assert pane.sort_mode == mode, f"cycle landed on {pane.sort_mode}, wanted {mode}"
            assert names(pane) == expected[mode], f"{mode}: {names(pane)}"
            await pilot.press("s")
            await pilot.pause()

        # The loop pressed `s` on its way out of "name", so the pane is on "size" now.
        await pilot.press("S")
        await pilot.pause()
        assert names(pane) == ["logs", "summary.json", "aaa.log", "capture.raw"], (
            f"reversed size did not run smallest first: {names(pane)}"
        )

        status = pane.query_one(".pane-status", app_mod.Static).render()
        assert "size ↑" in str(status), f"status does not show the sort: {status}"

        pane.table.move_cursor(row=names(pane).index("summary.json") + 1)
        await pilot.press("s")
        await pilot.pause()
        assert pane.current() is not None and pane.current().name == "summary.json", (
            "re-sorting moved the cursor off the entry it was on"
        )

        assert pane.sort_mode == "kind" and app.screen.local_pane.sort_mode == "name", (
            "sorting one pane changed the other"
        )
