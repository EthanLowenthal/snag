"""The path bar: typing, completion, and where `enter` lands the pane."""

from __future__ import annotations

from pathlib import Path

import pytest

import snag.app as app_mod

from helpers import settle, write


class PathBar:
    """The pieces of a browser screen these tests poke at, with the waiting folded in."""

    def __init__(self, pilot, screen, local: Path, remote: Path):
        self.pilot, self.screen = pilot, screen
        self.local, self.remote = local, remote
        self.pane = screen.remote_pane
        self.input = self.pane.path_input
        self.popup = self.pane.query_one(app_mod.Completions)
        self.base = str(remote) + "/"
        self.notes: list[str] = []  # whatever the app tried to notify about

    @property
    def value(self) -> str:
        return self.input.value

    def shown(self) -> list[str]:
        return [str(o.prompt) for o in self.popup.options]

    def hint(self) -> str:
        return str(self.screen.query_one("#hint", app_mod.Static).content)

    async def press(self, *keys: str) -> None:
        await self.pilot.press(*keys)
        await self.pilot.pause()

    async def write(self, text: str) -> None:
        await write(self.pilot, text)

    async def settle(self, predicate, tries: int = 400) -> bool:
        return await settle(self.pilot, predicate, tries)


@pytest.fixture
async def bar(remote_server):
    """A tree worth completing, with the remote pane focused: listings come over rsync."""
    local, remote = remote_server("p-box", big=1_000)
    (remote / "data" / "alpha" / "inner").mkdir(parents=True)
    (remote / "data" / "alpha-two").mkdir()
    (remote / "data" / "notes.txt").write_text("hi\n")
    (remote / ".secret").mkdir()

    app = app_mod.SnagApp()
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        connected = await settle(
            pilot,
            lambda: isinstance(app.screen, app_mod.BrowserScreen)
            and bool(app.screen.remote_pane.entries),
        )
        assert connected, "never connected and listed the remote"
        await pilot.press("tab")
        await pilot.pause()
        ui = PathBar(pilot, app.screen, local, remote)
        app.notify = lambda msg, **kw: ui.notes.append(str(msg))
        yield ui


async def test_opens_on_the_current_directory(bar):
    await bar.press("slash")
    assert bar.input.display and bar.input.has_focus, "'/' did not open the path bar"
    assert bar.value == bar.base, bar.value
    assert bar.hint() == bar.screen.PATH_HINT, bar.hint()
    assert bar.shown() == ["data/", "logs/", "capture.raw", "summary.json"], str(bar.shown())
    assert ".secret/" not in bar.shown(), "dotfiles offered before being asked for"

    await bar.write(".")
    assert bar.shown() == [".secret/", ".hidden"], str(bar.shown())


async def test_tab_completes_and_walks_candidates(bar):
    await bar.press("slash")
    await bar.write("da")
    await bar.press("tab")
    assert bar.value == bar.base + "data/", bar.value
    listed = await bar.settle(lambda: bar.shown() == ["alpha/", "alpha-two/", "notes.txt"])
    assert listed, f"completing a directory did not list it: {bar.shown()}"

    await bar.write("al")
    await bar.press("tab")
    assert bar.value == bar.base + "data/alpha", f"tab passed the shared prefix: {bar.value}"
    await bar.press("tab")
    assert bar.value == bar.base + "data/alpha/", bar.value
    assert bar.popup.highlighted == 0, f"candidate not marked: {bar.popup.highlighted}"

    await bar.press("down")
    assert bar.value == bar.base + "data/alpha-two/", bar.value
    await bar.press("up")
    assert bar.value == bar.base + "data/alpha/", bar.value

    await bar.press("tab")
    entered = await bar.settle(lambda: bar.shown() == ["inner/"])
    assert entered, f"tab on a candidate did not enter it: {bar.shown()}"
    assert bar.value == bar.base + "data/alpha/", bar.value
    await bar.write("in")
    await bar.press("tab")
    assert bar.value == bar.base + "data/alpha/inner/", bar.value

    await bar.press("enter")
    inner = str(bar.remote / "data" / "alpha" / "inner")
    assert await bar.settle(lambda: bar.pane.path == inner), bar.pane.path
    assert bar.input.display and bar.input.has_focus and bar.value == bar.pane.path + "/", (
        f"bar did not stay open, rooted where the pane went: {bar.value!r}"
    )


async def test_dot_dot_goes_up(bar):
    inner = str(bar.remote / "data" / "alpha" / "inner")
    await bar.press("slash")
    await bar.write("data/alpha/inner")
    await bar.press("enter")
    assert await bar.settle(lambda: bar.pane.path == inner), bar.pane.path

    # `..` needs no tab and no enter: it folds as the second dot is typed.
    await bar.write("..")
    alpha = str(bar.remote / "data" / "alpha")
    assert bar.value == alpha + "/", f"'..' was not folded into the parent: {bar.value}"
    assert await bar.settle(lambda: bar.shown() == ["inner/"]), str(bar.shown())
    await bar.press("enter")
    assert await bar.settle(lambda: bar.pane.path == alpha), bar.pane.path

    await bar.write("../notes.txt")
    await bar.press("enter")
    assert await bar.settle(lambda: bar.pane.path == str(bar.remote / "data")), bar.pane.path
    current = bar.pane.current()
    assert current is not None and current.name == "notes.txt", str(current)
    assert not bar.input.display and bar.pane.table.has_focus, (
        "landing on a file left the bar open"
    )


async def test_tilde_goes_home(bar):
    await bar.press("slash")  # take the pane somewhere else first
    await bar.write("data")
    await bar.press("enter")
    assert await bar.settle(lambda: bar.pane.path == str(bar.remote / "data")), bar.pane.path
    await bar.press("escape")

    await bar.press("tilde")
    assert bar.value == bar.base, f"'~' did not expand to home: {bar.value}"
    assert await bar.settle(lambda: "capture.raw" in bar.shown()), str(bar.shown())
    await bar.press("enter")
    assert await bar.settle(lambda: bar.pane.path == str(bar.remote)), bar.pane.path


async def test_enter_on_a_plain_directory(bar):
    """Nothing picked, nothing half-typed: enter just goes where the bar says."""
    await bar.press("slash")
    await bar.write("logs/")
    await bar.press("enter")
    logs = str(bar.remote / "logs")
    assert await bar.settle(lambda: bar.pane.path == logs), bar.pane.path
    assert [e.name for e in bar.pane.entries] == ["run.log"], str(bar.pane.entries)


async def test_enter_completes_and_refuses(bar):
    await bar.press("slash")
    await bar.write("data/")
    await bar.press("enter")
    data = str(bar.remote / "data")
    assert await bar.settle(lambda: bar.pane.path == data), bar.pane.path

    # "a" could be alpha or alpha-two, so enter should ask rather than pick.
    bar.notes.clear()
    await bar.write("a")
    await bar.press("enter")
    assert bar.pane.path == data, f"an ambiguous name moved the pane: {bar.pane.path}"
    assert bar.input.display, "an ambiguous name closed the bar"
    assert any("2 matches" in note for note in bar.notes), str(bar.notes)

    # "alpha-" can only be one thing, so enter finishes it.
    await bar.write("lpha-")
    await bar.press("enter")
    alpha_two = str(bar.remote / "data" / "alpha-two")
    assert await bar.settle(lambda: bar.pane.path == alpha_two), bar.pane.path

    # A name that is not there at all leaves the pane where it is.
    bar.notes.clear()
    await bar.write("nope-nope")
    await bar.press("enter")
    assert bar.pane.path == alpha_two, f"a dead path moved the pane: {bar.pane.path}"
    assert not bar.pane.status, f"pane left reporting an error: {bar.pane.status}"
    assert any("No such path" in note for note in bar.notes), str(bar.notes)


async def test_no_match_and_dismissal(bar):
    await bar.press("slash")
    await bar.write("nope-nope")
    assert not bar.popup.display, "a path matching nothing still showed a popup"

    await bar.press("escape")
    assert not bar.input.display and bar.pane.table.has_focus
    assert bar.pane.path == str(bar.remote), bar.pane.path
    assert bar.hint() == bar.screen.BROWSE_HINT, bar.hint()


async def test_clicking_a_match_takes_it(bar):
    await bar.pilot.press("slash")
    filled = await bar.settle(lambda: bar.popup.display and bar.popup.option_count > 1)
    await bar.pilot.click(bar.popup, offset=(2, 1))
    await bar.pilot.pause()
    assert filled and bar.value == bar.base + "logs/", bar.value


async def test_local_pane_completes_without_rsync(bar):
    await bar.press("tab")  # over to the local pane
    local_bar = bar.screen.local_pane.path_input
    await bar.press("slash")
    assert local_bar.value == str(bar.local) + "/", local_bar.value
    await bar.write("~")
    assert local_bar.value == str(Path.home()) + "/", local_bar.value
    await bar.press("escape")
