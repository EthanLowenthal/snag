"""The command line: what the arguments mean, and what snag does on arrival."""

from __future__ import annotations

from pathlib import Path

import pytest

import snag.app as app_mod
from snag.cli import CliError, expand_remote, parse
from snag.config import Server

from helpers import settle

SERVERS = [Server(name="node3", host="node3.example"), Server(name="box", host="box.example")]


def read(argv: list[str]):
    return parse(argv, servers=SERVERS)


# ------------------------------------------------------------------- parsing


def test_no_arguments_is_the_server_list():
    assert read([]) is None


def test_bare_name_opens_that_server():
    start = read(["node3"])
    assert start.server.name == "node3"
    assert (start.local, start.remote, start.copies) == (None, None, False)


def test_pull_points_both_panes_and_queues_the_copy(tmp_path):
    start = read(["node3:/data/runs/capture.raw", str(tmp_path)])
    assert start.server.name == "node3"
    assert start.from_remote is True
    assert start.sources == ("/data/runs/capture.raw",)
    assert (start.remote, start.remote_keep) == ("/data/runs", "capture.raw")
    assert (start.local, start.local_keep) == (str(tmp_path), None)
    assert start.probe is False, "a copy already says which part is the directory"


def test_push_reverses_the_direction(tmp_path):
    patch = tmp_path / "patch.diff"
    patch.write_text("--- a\n")
    start = read([str(patch), "box:/srv/incoming/"])
    assert start.server.name == "box"
    assert start.from_remote is False
    assert start.sources == (str(patch),)
    assert (start.local, start.local_keep) == (str(tmp_path), "patch.diff")
    assert start.remote == "/srv/incoming"


def test_several_sources_land_in_one_transfer(tmp_path):
    start = read(["node3:/data/a.bin", "node3:/data/b.bin", str(tmp_path)])
    assert start.sources == ("/data/a.bin", "/data/b.bin")
    assert start.remote == "/data"
    assert start.remote_keep is None, "no single entry to point the cursor at"


def test_relative_remote_paths_survive_for_the_connection(tmp_path):
    start = read(["node3:runs/x.txt", str(tmp_path)])
    assert start.sources == ("runs/x.txt",)
    assert start.remote == "runs", "expanded against the login directory later, not now"


def test_local_destination_is_expanded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "here").mkdir()
    start = read(["node3:/data/x", "here"])
    assert start.local == str(tmp_path / "here")


def test_remote_browse_asks_the_server_what_it_is():
    start = read(["node3:/data/runs"])
    assert (start.remote, start.probe, start.copies) == ("/data/runs", True, False)


def test_trailing_slash_settles_it_without_asking():
    start = read(["node3:/data/runs/"])
    assert (start.remote, start.probe) == ("/data/runs", False)


def test_remote_home_shorthands():
    for arg in ("node3:", "node3:~", "node3:."):
        start = read([arg])
        assert start.probe is False, arg
        assert start.remote in (None, "~", "."), arg


def test_local_browse_waits_for_a_server(tmp_path):
    start = read([str(tmp_path)])
    assert (start.server, start.local, start.local_keep) == (None, str(tmp_path), None)


def test_local_file_browses_its_directory(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("hi\n")
    start = read([str(target)])
    assert (start.local, start.local_keep) == (str(tmp_path), "notes.md")


@pytest.mark.parametrize(
    "argv, message",
    [
        (["nope:/data"], "no server named"),
        (["node3:/a", "box:/b", "/tmp"], "one server at a time"),
        (["node3:/a", "node3:/b"], "one side of a copy has to be a plain local path"),
        (["/tmp/one", "/tmp/two"], "neither side of that copy names a server"),
        (["node3:/a", "/tmp/x", "node3:/b"], "same side"),
        (["/definitely/not/here", "node3:/data"], "no such file or directory"),
    ],
)
def test_rejected(argv, message):
    with pytest.raises(CliError) as caught:
        read(argv)
    assert message in str(caught.value)


def test_a_shorthand_that_can_only_mean_one_server(tmp_path):
    hosts = [Server(name="build01.example.com"), Server(name="nas.example.com")]
    start = parse(["01:/data/x", str(tmp_path)], servers=hosts)
    assert start.server.name == "build01.example.com", "no substring match"
    assert parse(["nas"], servers=hosts).server.name == "nas.example.com", "no prefix match"

    with pytest.raises(CliError) as caught:
        parse(["example:/data/x", str(tmp_path)], servers=hosts)
    assert "could mean any of: build01.example.com, nas.example.com" in str(caught.value)


def test_an_exact_name_beats_a_shorthand(tmp_path):
    hosts = [Server(name="app1"), Server(name="app16")]
    assert parse(["app1:/data", str(tmp_path)], servers=hosts).server.name == "app1"


def test_a_local_path_may_hold_a_colon(tmp_path):
    odd = tmp_path / "12:30.log"
    odd.write_text("x\n")
    start = read([str(odd)])
    assert (start.server, start.local_keep) == (None, "12:30.log")


def test_expand_remote():
    assert expand_remote("/home/e", "runs/x") == "/home/e/runs/x"
    assert expand_remote("/home/e", "~/runs") == "/home/e/runs"
    assert expand_remote("/home/e", "") == "/home/e"
    assert expand_remote("/home/e", "/data/../data/runs") == "/data/runs"


# ------------------------------------------------------------------------ ui


async def connected(app, pilot) -> bool:
    return await settle(
        pilot,
        lambda: isinstance(app.screen, app_mod.BrowserScreen)
        and bool(app.screen.remote_pane.entries),
    )


async def test_pull_from_the_command_line(remote_server):
    local, remote = remote_server("cli-box", big=4_000_000)
    start = parse([f"cli-box:{remote}/capture.raw", str(local)])
    app = app_mod.SnagApp(start)

    async with app.run_test(size=(110, 32)) as pilot:
        assert await connected(app, pilot), "never reached the browser"
        screen = app.screen
        assert screen.remote_pane.path == str(remote), screen.remote_pane.path
        assert screen.local_pane.path == str(local), screen.local_pane.path
        cursor = screen.remote_pane.current()
        assert cursor is not None and cursor.name == "capture.raw", "cursor left elsewhere"

        row = next(iter(screen.query(app_mod.TransferRow)), None)
        assert row is not None, "the copy never started"
        assert row.label_text == "← capture.raw", row.label_text
        assert await settle(pilot, lambda: not row.active, tries=600), "copy never finished"
        assert (local / "capture.raw").stat().st_size == 4_000_000
        assert not (local / "summary.json").exists(), "copied more than it was asked to"

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, app_mod.ServerScreen), "escape lost the server list"


async def test_push_from_the_command_line(remote_server, tmp_path):
    local, remote = remote_server("push-box", big=1000)
    patch = local / "patch.diff"
    patch.write_text("--- a\n+++ b\n")
    dest = remote / "incoming"
    dest.mkdir()

    start = parse([str(patch), f"push-box:{dest}"])
    app = app_mod.SnagApp(start)
    async with app.run_test(size=(110, 32)) as pilot:
        assert await connected(app, pilot), "never reached the browser"
        screen = app.screen
        assert screen.remote_pane.path == str(dest), screen.remote_pane.path
        assert screen.local_pane.path == str(local), screen.local_pane.path

        row = next(iter(screen.query(app_mod.TransferRow)), None)
        assert row is not None and row.label_text == "→ patch.diff", row and row.label_text
        assert await settle(pilot, lambda: not row.active, tries=600), "copy never finished"
        assert (dest / "patch.diff").read_text() == patch.read_text()


async def test_a_named_directory_is_opened_and_a_named_file_is_pointed_at(remote_server):
    local, remote = remote_server("probe-box", big=1000)

    for argument, expected_path, expected_cursor in [
        (f"probe-box:{remote}/logs", str(remote / "logs"), None),
        (f"probe-box:{remote}/summary.json", str(remote), "summary.json"),
    ]:
        app = app_mod.SnagApp(parse([argument]))
        async with app.run_test(size=(110, 32)) as pilot:
            assert await connected(app, pilot), f"never listed anything for {argument}"
            pane = app.screen.remote_pane
            assert pane.path == expected_path, f"{argument} landed on {pane.path}"
            cursor = pane.current()
            assert (cursor.name if cursor else None) == expected_cursor, argument
            assert not list(app.screen.query(app_mod.TransferRow)), "browsing started a copy"


async def test_a_local_path_alone_survives_picking_a_server(remote_server, tmp_path):
    local, remote = remote_server("later-box", big=1000)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    app = app_mod.SnagApp(parse([str(elsewhere)]))
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, app_mod.ServerScreen), "went somewhere without a server"
        await pilot.press("enter")
        assert await connected(app, pilot), "never reached the browser"
        assert app.screen.local_pane.path == str(elsewhere), app.screen.local_pane.path
        assert app.take_start() is None, "the command line was applied twice"


def test_entry_point_reports_a_bad_command_line(capsys, monkeypatch):
    from snag import __main__ as main_mod

    monkeypatch.setattr(main_mod.shutil, "which", lambda name: "/usr/bin/rsync")
    assert main_mod.main(["no-such-server:/data", str(Path.home())]) == 2
    assert "no server named" in capsys.readouterr().err

    assert main_mod.main(["--help"]) == 0
    assert "snag SOURCE... DEST" in capsys.readouterr().out
