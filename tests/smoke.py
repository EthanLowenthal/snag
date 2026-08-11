#!/usr/bin/env python
"""End-to-end checks for snag. Run with: .venv/bin/python tests/smoke.py

No test framework and no network: the "remote" side is a local directory reached through
a patched `_remote_spec`, so real rsync processes and the real UI are exercised while
everything stays on disk. Exits non-zero on the first failure.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="snag-smoke-"))
os.environ["XDG_CONFIG_HOME"] = str(ROOT / "config")
os.environ["XDG_STATE_HOME"] = str(ROOT / "state")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import snag.rsync as rsync_mod  # noqa: E402

rsync_mod._remote_spec = lambda server, path: path  # "remote" paths are local paths

import snag.app as app_mod  # noqa: E402
from snag.config import Server, parse_ssh_config, save_servers, with_remembered  # noqa: E402
from snag.rsync import RsyncError, Transfer, local_list, measure  # noqa: E402

app_mod.remote_list = lambda server, path: local_list(Path(path))

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


class Throttled(Transfer):
    """A transfer slow enough that intermediate progress can be observed."""

    @property
    def command(self) -> list[str]:
        base = super().command
        return [base[0], "--bwlimit=2500", *base[1:]]


def make_tree(name: str, big: int = 20_000_000) -> tuple[Path, Path]:
    local, remote = ROOT / name / "local", ROOT / name / "remote"
    (remote / "logs").mkdir(parents=True)
    local.mkdir(parents=True)
    (remote / "capture.raw").write_bytes(os.urandom(big))
    (remote / "summary.json").write_text('{"ok": true}\n')
    (remote / ".hidden").write_text("x\n")
    (remote / "logs" / "run.log").write_text("line\n" * 200)
    return local, remote


# ---------------------------------------------------------------- unit-ish


def test_ssh_config() -> None:
    print("\nssh_config parsing")
    cfg = ROOT / "ssh_config"
    (ROOT / "extra").mkdir()
    (ROOT / "extra" / "more").write_text("Host imported\n  HostName 10.9.9.9\n")
    cfg.write_text(
        "Host *\n  ForwardAgent yes\n"
        "Host alpha\n  HostName alpha.example.com\n  User ada\n  Port 2222\n"
        "Host beta gamma\n  HostName shared.example.com\n"
        f"Include {ROOT / 'extra' / 'more'}\n"
    )
    servers = {s.name: s for s in parse_ssh_config(cfg)}
    check("wildcard Host * skipped", "*" not in servers, f"found {sorted(servers)}")
    check("fields parsed", servers["alpha"].detail == "ada@alpha.example.com:2222",
          servers["alpha"].detail)
    check("multi-name Host expands", {"beta", "gamma"} <= servers.keys())
    check("Include followed", "imported" in servers)
    check("all marked as ssh-sourced", all(s.source == "ssh" for s in servers.values()))


def test_stale_path_fallback() -> None:
    print("\nremembered-path fallback")
    server = Server(name="ghost", local=str(ROOT), remote="/data")
    save_servers([server])
    from snag import config as cfg_mod

    cfg_mod.remember(server, "/definitely-not-here-xyz", "/data/runs")
    resolved = with_remembered(server)
    check("dead local path ignored", resolved.local == str(ROOT), resolved.local)
    check("remote path still remembered", resolved.remote == "/data/runs", resolved.remote)


def test_transfer_engine() -> None:
    print("\ntransfer engine (real rsync)")
    local, remote = make_tree("engine", big=40_000_000)
    server = Server(name="fake")
    sources = [str(remote / "capture.raw"), str(remote / "logs"), str(remote / "summary.json")]

    expected = 40_000_000 + 13 + len("line\n") * 200
    total = measure(server, True, sources)
    check("measure() sums the selection", total == expected, f"{total:,} vs {expected:,}")

    samples: list[tuple[int, str]] = []
    transfer = Throttled(server, from_remote=True, sources=sources, dest=str(local), total=total)
    transfer.run(lambda p: samples.append((p.done, p.eta)))

    dones = [d for d, _ in samples]
    check("progress is monotonic", all(a <= b for a, b in zip(dones, dones[1:])))
    check("progress reaches the total", dones[-1] == total, f"{dones[-1]:,}/{total:,}")
    check("several intermediate updates", len(samples) > 5, f"{len(samples)} callbacks")

    etas = [e for _, e in samples if e]
    check("overall ETA decreases", etas == sorted(etas, reverse=True), f"{etas[:4]}…")

    on_disk = sum(f.stat().st_size for f in local.rglob("*") if f.is_file())
    check("all bytes landed", on_disk == total, f"{on_disk:,}")
    check("directory source recursed", (local / "logs" / "run.log").exists())


def test_error_messages() -> None:
    print("\nerror reporting")
    dead = Server(name="dead", host="127.0.0.1", port=1)
    unpatched = rsync_mod._remote_spec
    rsync_mod._remote_spec = lambda s, p: f"{s.target}:{p}"
    try:
        rsync_mod.remote_list(dead, "/data")
        check("unreachable host raises", False, "no error raised")
    except RsyncError as exc:
        message = str(exc)
        check("error names the real cause", "connect to host" in message, message[:60])
        check("rsync noise filtered out", "unexpected end of file" not in message)
    finally:
        rsync_mod._remote_spec = unpatched


# ------------------------------------------------------------------- the UI


async def test_ui() -> None:
    print("\nUI end to end")
    local, remote = make_tree("ui", big=8_000_000)
    app_mod.remote_home = lambda server: str(remote)
    save_servers([Server(name="fake-box", host="localhost", remote=".", local=str(local))])

    app = app_mod.SnagApp()
    notes: list[tuple[str, str | None]] = []

    async def settle(predicate, tries: int = 400) -> bool:
        for _ in range(tries):
            await pilot.pause(0.05)
            if predicate():
                return True
        return False

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        check("server list populated", app.screen.query_one("#servers").row_count >= 1)

        await pilot.press("enter")
        ok = await settle(
            lambda: isinstance(app.screen, app_mod.BrowserScreen)
            and bool(app.screen.remote_pane.entries)
        )
        check("connected and listed the remote", ok)
        screen = app.screen
        app.notify = lambda msg, **kw: notes.append((str(msg), kw.get("severity")))
        rows = lambda pane: [e.name for e in pane.rows[1:]]  # noqa: E731

        check("remote home resolved", screen.remote_pane.path == str(remote))
        check("dotfiles hidden by default", ".hidden" not in rows(screen.remote_pane))

        await pilot.press("tab")
        await pilot.pause()
        check("tab moves focus across panes", screen.focused_pane is screen.remote_pane)

        await pilot.press("full_stop")
        await pilot.pause()
        check("'.' reveals dotfiles", ".hidden" in rows(screen.remote_pane))
        await pilot.press("full_stop")
        await pilot.pause()

        remote_pane = screen.remote_pane
        remote_pane.table.move_cursor(row=rows(remote_pane).index("logs") + 1)
        await pilot.press("enter")
        await settle(lambda: remote_pane.path.endswith("logs"))
        check("descends into a directory", rows(remote_pane) == ["run.log"])
        await pilot.press("backspace")
        await settle(lambda: remote_pane.path == str(remote))
        check("returns cursor to the exited directory",
              remote_pane.current() is not None and remote_pane.current().name == "logs")

        remote_pane.table.move_cursor(row=rows(remote_pane).index("capture.raw") + 1)
        await pilot.press("space")
        remote_pane.table.move_cursor(row=rows(remote_pane).index("summary.json") + 1)
        await pilot.press("space")
        await pilot.pause()
        check("marks tracked", remote_pane.marked == {"capture.raw", "summary.json"})

        await pilot.press("c")
        await pilot.pause()
        row = next(iter(screen.query(app_mod.TransferRow)), None)
        check("transfer row appears", row is not None and row.label_text == "← 2 items",
              row.label_text if row else "none")
        await settle(lambda: row is not None and not row.active)
        check("transfer completes", row is not None and not row.active)
        check("bar ends full", row.query_one(app_mod.ProgressBar).progress == 100)

        await settle(lambda: "capture.raw" in rows(screen.local_pane))
        check("destination pane refreshed", "capture.raw" in rows(screen.local_pane))
        check("marked bytes copied", (local / "capture.raw").stat().st_size == 8_000_000)
        check("unmarked entry left alone", not (local / "logs").exists())

        notes.clear()
        await pilot.press("tab")  # back to the local pane, which has no marks
        screen.local_pane.table.move_cursor(row=0)  # the ".." row
        await pilot.press("c")
        await pilot.pause()
        check("copying nothing is refused",
              any("Nothing selected" in n[0] for n in notes), str(notes))
        check("no stray transfer started",
              len(list(screen.query(app_mod.TransferRow))) == 1,
              f"{len(list(screen.query(app_mod.TransferRow)))} rows")

        notes.clear()
        await pilot.press("x")
        await pilot.pause()
        check("cancel with no transfer warns",
              any("No transfer" in n[0] for n in notes), str(notes))

        await pilot.press("X")
        await pilot.pause()
        panel = screen.query_one(app_mod.TransferPanel)
        check("finished rows cleared", not list(panel.query(app_mod.TransferRow)))
        check("empty transfer panel hides", panel.display is False)

        await pilot.press("escape")
        await pilot.pause()
        check("escape returns to the server list",
              isinstance(app.screen, app_mod.ServerScreen))

    state = (ROOT / "state" / "snag" / "state.json").read_text()
    check("directories remembered on exit", "fake-box" in state and str(local) in state)


async def test_cancel_and_resume() -> None:
    print("\ncancel and resume")
    size = 24_000_000
    local, remote = make_tree("cancel", big=size)
    app_mod.remote_home = lambda server: str(remote)
    save_servers([Server(name="c-box", host="localhost", remote=".", local=str(local))])

    original = app_mod.Transfer
    app_mod.Transfer = Throttled
    app = app_mod.SnagApp()
    try:
        async with app.run_test(size=(110, 32)) as pilot:

            async def settle(predicate, tries: int = 600) -> bool:
                for _ in range(tries):
                    await pilot.pause(0.05)
                    if predicate():
                        return True
                return False

            await pilot.pause()
            await pilot.press("enter")
            await settle(
                lambda: isinstance(app.screen, app_mod.BrowserScreen)
                and bool(app.screen.remote_pane.entries)
            )
            screen = app.screen
            await pilot.press("tab")
            names = [e.name for e in screen.remote_pane.rows[1:]]
            screen.remote_pane.table.move_cursor(row=names.index("capture.raw") + 1)
            await pilot.press("c")

            row = None

            def started() -> bool:
                nonlocal row
                row = next(iter(screen.query(app_mod.TransferRow)), None)
                return row is not None and (row.query_one(app_mod.ProgressBar).progress or 0) > 15

            check("transfer got underway", await settle(started))
            await pilot.press("x")
            check("cancel stops it", await settle(lambda: not row.active))

            partial = (local / "capture.raw")
            landed = partial.stat().st_size if partial.exists() else 0
            check("a partial file is kept for resume", 0 < landed < size, f"{landed:,} bytes")

            app_mod.Transfer = original  # full speed for the resume
            screen.remote_pane.table.move_cursor(row=names.index("capture.raw") + 1)
            await pilot.press("c")
            second = None

            def finished() -> bool:
                nonlocal second
                all_rows = list(screen.query(app_mod.TransferRow))
                second = all_rows[-1] if len(all_rows) > 1 else None
                return second is not None and not second.active

            check("resumed transfer completes", await settle(finished))
            check("file is whole again", partial.stat().st_size == size,
                  f"{partial.stat().st_size:,}")
            check("content matches byte for byte",
                  partial.read_bytes() == (remote / "capture.raw").read_bytes())
    finally:
        app_mod.Transfer = original


def main() -> int:
    print(f"snag smoke tests (scratch: {ROOT})")
    test_ssh_config()
    test_stale_path_fallback()
    test_transfer_engine()
    test_error_messages()
    asyncio.run(test_ui())
    asyncio.run(test_cancel_and_resume())

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("all checks passed")
    shutil.rmtree(ROOT, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
