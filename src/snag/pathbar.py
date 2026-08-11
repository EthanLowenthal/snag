"""The path bar: bash's habits for getting somewhere — type it, tab it, let `~` mean home.

Both sides of the browser use `/` separators (the local side is POSIX too), so every
path in here goes through `posixpath` no matter which pane asked. Nothing in this module
touches the filesystem or the network: listings come from the owning pane, which knows
whether they are a `scandir` away or an rsync away.
"""

from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING

from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

from .rsync import Entry

if TYPE_CHECKING:
    from .app import FilePane

MAX_MATCHES = 200


def entry_style(entry: Entry) -> str:
    """Directories blue, links purple — the same colours the tables use."""
    if entry.is_dir:
        return "bold #7dcfff"
    if entry.is_link:
        return "italic #bb9af7"
    return ""


def common_prefix(names: list[str]) -> str:
    """The longest start every name shares, which is how far a tab press can complete."""
    shortest = min(names, key=len)
    for index, char in enumerate(shortest):
        if any(name[index] != char for name in names):
            return shortest[:index]
    return shortest


class Completions(OptionList, can_focus=False):
    """The popup under the path bar. Focus stays in the input; this is display only."""

    ALLOW_SELECT = False

    def on_mount(self) -> None:
        self.display = False

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()  # the highlight is driven from the input; nobody outside cares


class PathInput(Input):
    """A path field with bash's reflexes: tab completion, `~` expansion, a popup of matches.

    Everything it knows about a directory comes from the pane that owns it, so the same
    widget drives the local side (listings are instant) and the remote one (listings are
    fetched in the background, and the popup fills in when they land).
    """

    BINDINGS = [
        Binding("escape", "close", "Cancel", show=False),
        Binding("down,ctrl+n", "cycle(1)", "Next match", show=False),
        Binding("up,ctrl+p", "cycle(-1)", "Previous match", show=False),
    ]

    class Go(Message):
        """A resolved destination. `keep` is a file to leave the cursor on, if any."""

        def __init__(self, path: str, keep: str | None = None) -> None:
            super().__init__()
            self.path = path
            self.keep = keep

    class Opened(Message):
        """The bar was opened, so the screen can say which keys mean what now."""

    class Closed(Message):
        """The bar was dismissed, so the pane should take its focus back."""

    def __init__(self, pane: FilePane, **kwargs) -> None:
        # No select-on-focus: the bar opens holding the current directory, and a
        # selected-then-replaced path would throw that away on the first keystroke.
        super().__init__(
            placeholder="path — tab completes, ~ is home",
            select_on_focus=False,
            **kwargs,
        )
        self.pane = pane
        self.display = False
        # (head, matches, index) while walking candidates; cleared by any real edit.
        self._cycle: tuple[str, list[Entry], int] | None = None
        self._echo: str | None = None  # a value we set ourselves, not something typed
        self._tab_pending = False  # a tab press waiting on a listing to arrive

    @property
    def popup(self) -> Completions:
        return self.pane.query_one(Completions)

    # ------------------------------------------------------------ open/close

    def open(self, value: str = "") -> None:
        self._cycle = None
        self._tab_pending = False
        self.display = True
        self._set(value)
        self.focus()
        self._show(self._candidates())
        # The field has no width until this layout lands, so the scroll that keeps the
        # cursor in view has nothing to work with yet. Nudge it once it does.
        self.call_after_refresh(self._reveal_cursor)
        self.post_message(self.Opened())

    def _reveal_cursor(self) -> None:
        self.cursor_position = 0
        self.cursor_position = len(self.value)

    def action_close(self) -> None:
        self.display = False
        self.popup.display = False
        self._cycle = None
        self._tab_pending = False
        self._set("")
        self.post_message(self.Closed())

    def _set(self, value: str) -> None:
        """Write the field ourselves, flagging it so the change is not read as an edit."""
        self._echo = value if value != self.value else None
        self.value = value
        self.cursor_position = len(value)

    # -------------------------------------------------------------- matching

    def _split(self) -> tuple[str, str]:
        """The value as (everything through the last `/`, the partial name after it)."""
        head, slash, tail = self.value.rpartition("/")
        return head + slash, tail

    def _resolve(self, text: str) -> str | None:
        """Expand `~`, `$VAR`s and relative parts. None while a remote home is unknown."""
        if text.startswith("~"):
            home = self.pane.completion_home()
            if home is None:
                return None
            text = posixpath.join(home, text[1:].lstrip("/"))
        elif not text.startswith("/"):
            text = posixpath.join(self.pane.path, text)
        if not self.pane.is_remote:
            text = posixpath.expandvars(text)
        return posixpath.normpath(text)

    def _candidates(self) -> list[Entry] | None:
        """Entries the partial name could become; None while its listing is still loading."""
        head, tail = self._split()
        directory = self._resolve(head)
        if directory is None:
            return None
        entries = self.pane.completion_entries(directory)
        if entries is None:
            return None
        # bash only offers dotfiles once you have typed the dot, and so do we.
        hidden_ok = tail.startswith(".")
        pool = [e for e in entries if hidden_ok or not e.name.startswith(".")]
        found = [e for e in pool if e.name.startswith(tail)]
        if not found and tail:
            lowered = tail.lower()  # the local disk is likely case-insensitive; act like it
            found = [e for e in pool if e.name.lower().startswith(lowered)]
        return found

    def _show(self, matches: list[Entry] | None, highlight: int | None = None) -> None:
        popup = self.popup
        popup.clear_options()
        if matches is None:
            popup.add_option(Option(Text("listing…", style="dim italic"), disabled=True))
        elif not matches:
            popup.display = False
            return
        else:
            popup.add_options(
                Option(Text(e.name + ("/" if e.is_dir else ""), style=entry_style(e)))
                for e in matches[:MAX_MATCHES]
            )
            if len(matches) > MAX_MATCHES:
                extra = len(matches) - MAX_MATCHES
                popup.add_option(Option(Text(f"… {extra} more", style="dim"), disabled=True))
        popup.display = True
        popup.highlighted = highlight

    def _expand_tilde(self) -> None:
        """Swap a `~` for the real home, so the bar always shows a path that exists.

        A typed `~` starts the path over from home wherever it lands, which is the way
        out of the long path the bar opens with. Only the character just typed counts, so
        a completed name that happens to end in `~` keeps it.
        """
        cursor = self.cursor_position
        if self.value.startswith("~"):
            restart, rest = False, self.value[1:]
        elif cursor and self.value[cursor - 1] == "~":
            restart, rest = True, self.value[cursor:]
        else:
            return
        home = self.pane.completion_home()
        if home is None:
            return  # a remote we have not asked yet; the answer will land in a moment
        head = home.rstrip("/") + "/"
        self._set(head + rest.lstrip("/"))
        if restart:
            self.cursor_position = len(head)  # carry on typing where the `~` was

    def listings_arrived(self) -> None:
        """A background listing landed: refill the popup, and finish a pending tab press."""
        if not self.display or self._cycle is not None:
            return
        self._expand_tilde()
        matches = self._candidates()
        self._show(matches)
        if self._tab_pending and matches is not None:
            self._tab_pending = False
            self.action_complete()

    # ------------------------------------------------------------ completion

    def action_complete(self, step: int = 1) -> None:
        """Tab only ever moves forward: extend the name, then step into what it names.

        With a candidate highlighted, tab takes it and carries on inside it, so a deep
        path is tab-tab-tab the way it is in a shell. Picking a different candidate at
        the same level is what the arrows are for.
        """
        if self._cycle is not None:
            self._cycle = None  # take the highlighted candidate and complete within it
            self._show(self._candidates())
            return
        if self._fold_up():
            return
        matches = self._candidates()
        if matches is None:
            self._tab_pending = True  # complete as soon as the listing arrives
            self._show(None)
            return
        if not matches:
            return
        head, tail = self._split()
        shared = common_prefix([e.name for e in matches])
        if len(shared) > len(tail):
            only = matches[0] if len(matches) == 1 else None
            self._set(head + shared + ("/" if only is not None and only.is_dir else ""))
            self._show(self._candidates())
            return
        self._start_cycle(step)

    def _fold_up(self, include_dot: bool = True) -> bool:
        """Collapse a trailing `..` (or `.`) into a real path, to carry on a level up.

        `include_dot` is off while typing: a lone `.` there is someone reaching for a
        dotfile, and folding it away would take the matches back out from under them.
        """
        tokens = ("..", ".") if include_dot else ("..",)
        if self.value.rstrip("/").rpartition("/")[2] not in tokens:
            return False
        folded = self._resolve(self.value)
        if folded is None:
            self._tab_pending = True  # a `~` we cannot expand until the home lands
            return True
        self._set(folded.rstrip("/") + "/")
        self._show(self._candidates())
        return True

    def choose(self, index: int) -> None:
        """Take a specific candidate — what a click on the popup means."""
        head, matches = (
            self._cycle[0:2] if self._cycle is not None else (self._split()[0], self._candidates())
        )
        if not matches or index >= len(matches):
            return
        entry = matches[index]
        self._cycle = None
        self._set(head + entry.name + ("/" if entry.is_dir else ""))
        self._show(self._candidates())
        self.focus()

    def action_cycle(self, step: int) -> None:
        """Arrows walk the candidates at this level, writing each one into the bar."""
        if self._cycle is None:
            self._start_cycle(step)
        else:
            self._advance(step)

    def _start_cycle(self, step: int) -> None:
        matches = self._candidates()
        if not matches:
            return
        self._cycle = (self._split()[0], matches, -1 if step > 0 else 0)
        self._advance(step)

    def _advance(self, step: int) -> None:
        assert self._cycle is not None
        head, matches, index = self._cycle
        index = (index + step) % len(matches)
        entry = matches[index]
        self._set(head + entry.name + ("/" if entry.is_dir else ""))
        self._show(matches, highlight=index)
        self._cycle = (head, matches, index)

    # --------------------------------------------------------------- editing

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        if event.value == self._echo:
            self._echo = None
            return
        self._echo = None
        self._cycle = None  # a real edit ends the walk through candidates
        self._tab_pending = False
        self._expand_tilde()
        if self._fold_up(include_dot=False):
            return  # `..` takes effect as it is typed; no need to press anything
        self._show(self._candidates())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter moves the pane. On a directory the bar stays open, now rooted there.

        That keeps a wrong turn one `..` away from being undone, and makes the bar a way
        to walk a tree rather than a one-shot jump. Escape is how you put it away.

        A half-typed name goes where it can only have meant; a name that matches nothing
        is refused instead of dragging the pane onto a path that is not there.
        """
        event.stop()
        text = self.value.strip()
        if not text:
            self.action_close()
            return
        target = self._resolve(text)
        if target is None:
            return  # still waiting on the remote home, so `~` cannot be expanded yet
        directory, name = posixpath.split(target)
        # A listing still in flight leaves us no way to check: go, and let the pane report.
        entries = self.pane.completion_entries(directory)
        match = None if entries is None else next((e for e in entries if e.name == name), None)
        if entries is not None and match is None and name:
            matches = self._candidates() or []
            if len(matches) != 1:
                self.notify(
                    f"{len(matches)} matches — pick one" if matches else f"No such path: {text}",
                    severity="warning",
                )
                return
            match = matches[0]
            name = match.name
            target = posixpath.join(directory, name)
        if match is not None and not (match.is_dir or match.is_link):
            self.post_message(self.Go(directory, keep=name))  # a file: land on it
            self.action_close()
            return
        self.post_message(self.Go(target))
        self._cycle = None
        self._set(target.rstrip("/") + "/")  # keep typing from where the pane is going
        # The pane is on its way to `target`; its listing will fill the popup back in.
        self._show(None)
