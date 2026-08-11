"""The snag TUI: a server list, then two file panes with a transfer queue underneath."""

from __future__ import annotations

import posixpath
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    Static,
)

from . import config
from .config import Server
from .rsync import Entry, Progress, RsyncError, Transfer, local_list, measure, remote_home, remote_list

UNITS = ["B", "K", "M", "G", "T", "P"]


def format_size(size: int) -> str:
    value = float(size)
    for unit in UNITS:
        if value < 1024 or unit == UNITS[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}"


def format_mtime(stamp: float | None) -> str:
    if not stamp:
        return ""
    when = datetime.fromtimestamp(stamp)
    same_year = when.year == datetime.now().year
    return when.strftime("%b %d %H:%M" if same_year else "%b %d  %Y")


# ------------------------------------------------------------------- panes


class FilePane(Vertical):
    """One side of the browser: a path, its entries, and which of them are marked."""

    class PathChanged(Message):
        """Emitted on navigation so the screen can remember where we ended up."""

        def __init__(self, side: str, path: str) -> None:
            super().__init__()
            self.side = side
            self.path = path

    def __init__(self, side: str, server: Server, path: str, autoload: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.side = side
        self.server = server
        self.path = path
        self.autoload = autoload
        self.entries: list[Entry] = []
        self.rows: list[Entry | None] = []  # index 0 is the ".." row
        self.marked: set[str] = set()
        self.show_hidden = False
        self.status = "connecting…" if not autoload else ""
        self._generation = 0

    @property
    def is_remote(self) -> bool:
        return self.side == "remote"

    @property
    def table(self) -> DataTable:
        return self.query_one(DataTable)

    def compose(self) -> ComposeResult:
        yield Static("", classes="pane-title")
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Static("", classes="pane-status")

    def on_mount(self) -> None:
        table = self.table
        table.add_column(" ", width=1)
        table.add_column("Name")
        table.add_column(Text("Size", justify="right"), width=9)
        table.add_column("Modified", width=13)
        self._render_chrome()
        if self.autoload:
            self.reload()

    # ---------------------------------------------------------- rendering

    def _render_chrome(self) -> None:
        label = "LOCAL" if not self.is_remote else f"REMOTE · {self.server.name}"
        title = Text(f" {label} ", style="reverse bold")
        title.append(f" {self.path}", style="none")
        self.query_one(".pane-title", Static).update(title)

        bits = []
        if self.status:
            bits.append(self.status)
        else:
            visible = len(self.rows) - 1
            hidden = len(self.entries) - visible
            bits.append(f"{visible} items" + (f" · {hidden} hidden" if hidden > 0 else ""))
            if self.marked:
                marked_bytes = sum(e.size for e in self.entries if e.name in self.marked)
                bits.append(f"[{len(self.marked)} marked · {format_size(marked_bytes)}]")
        self.query_one(".pane-status", Static).update(Text(" " + "  ".join(bits), style="dim"))

    def _cells(self, entry: Entry) -> tuple:
        if entry.is_dir:
            style = "bold #7dcfff"
        elif entry.is_link:
            style = "italic #bb9af7"
        else:
            style = ""
        name = Text(entry.name + ("/" if entry.is_dir else ""), style=style, no_wrap=True)
        size = Text("—" if entry.is_dir else format_size(entry.size), justify="right", style="dim")
        return (self._mark_cell(entry), name, size, Text(format_mtime(entry.mtime), style="dim"))

    def _mark_cell(self, entry: Entry) -> Text:
        return Text("▌", style="bold #e0af68") if entry.name in self.marked else Text(" ")

    def _visible(self) -> list[Entry]:
        if self.show_hidden:
            return self.entries
        return [e for e in self.entries if not e.name.startswith(".")]

    def _populate(self, keep: str | None = None) -> None:
        table = self.table
        table.clear()
        self.rows = [None]
        table.add_row(Text(" "), Text("..", style="dim bold"), Text(""), Text(""))
        for entry in self._visible():
            self.rows.append(entry)
            table.add_row(*self._cells(entry))
        index = 0
        if keep:
            index = next(
                (i for i, e in enumerate(self.rows) if e is not None and e.name == keep), 0
            )
        if self.rows:
            table.move_cursor(row=min(index, len(self.rows) - 1))
        self._render_chrome()

    # -------------------------------------------------------------- loading

    def reload(self, keep: str | None = None) -> None:
        self.status = "loading…"
        self._render_chrome()
        self._generation += 1
        self._load(self._generation, keep)

    @work(thread=True)
    def _load(self, generation: int, keep: str | None) -> None:
        try:
            if self.is_remote:
                entries = remote_list(self.server, self.path)
            else:
                entries = local_list(Path(self.path))
            error = ""
        except RsyncError as exc:
            entries, error = [], str(exc)
        self.app.call_from_thread(self._loaded, generation, entries, error, keep)

    def _loaded(self, generation: int, entries: list[Entry], error: str, keep: str | None) -> None:
        if generation != self._generation:
            return  # a newer navigation already superseded this listing
        self.entries = entries
        self.marked.clear()
        self.status = error
        self._populate(keep)
        if error:
            self.app.notify(error, title=f"{self.side} listing failed", severity="error")

    # ----------------------------------------------------------- navigation

    def abs_path(self, entry: Entry) -> str:
        if self.is_remote:
            return posixpath.normpath(posixpath.join(self.path, entry.name))
        return str(Path(self.path) / entry.name)

    def current(self) -> Entry | None:
        row = self.table.cursor_row
        return self.rows[row] if 0 <= row < len(self.rows) else None

    def selection(self) -> list[Entry]:
        """Marked entries if there are any, otherwise whatever the cursor is on."""
        if self.marked:
            return [e for e in self._visible() if e.name in self.marked]
        entry = self.current()
        return [entry] if entry else []

    def go_to(self, path: str, keep: str | None = None) -> None:
        self.path = path
        self.post_message(self.PathChanged(self.side, path))
        self.reload(keep)

    def go_up(self) -> None:
        """Step to the parent, leaving the cursor on the directory we came out of."""
        if self.is_remote:
            trimmed = self.path.rstrip("/")
            parent, leaving = posixpath.dirname(trimmed) or "/", posixpath.basename(trimmed)
        else:
            parent, leaving = str(Path(self.path).parent), Path(self.path).name
        if parent != self.path:
            self.go_to(parent, keep=leaving)

    def open_current(self) -> None:
        entry = self.current()
        if entry is None:
            self.go_up()
        elif entry.is_dir or entry.is_link:
            self.go_to(self.abs_path(entry))

    def toggle_mark(self) -> None:
        entry = self.current()
        if entry is None:
            return
        self.marked.symmetric_difference_update({entry.name})
        row = self.table.cursor_row
        self.table.update_cell_at(Coordinate(row, 0), self._mark_cell(entry))
        if row + 1 < len(self.rows):
            self.table.move_cursor(row=row + 1)
        self._render_chrome()

    def mark_all(self) -> None:
        visible = self._visible()
        self.marked = set() if self.marked else {e.name for e in visible}
        for index, entry in enumerate(visible, start=1):
            self.table.update_cell_at(Coordinate(index, 0), self._mark_cell(entry))
        self._render_chrome()

    def toggle_hidden(self) -> None:
        self.show_hidden = not self.show_hidden
        entry = self.current()
        self._populate(keep=entry.name if entry else None)


# --------------------------------------------------------------- transfers


class TransferRow(Horizontal):
    """A single queued/running rsync with its progress bar."""

    def __init__(self, label: str, transfer: Transfer, **kwargs):
        super().__init__(**kwargs)
        self.label_text = label
        self.transfer = transfer
        self.active = True

    def compose(self) -> ComposeResult:
        yield Static(Text(self.label_text, no_wrap=True), classes="xfer-label")
        yield ProgressBar(total=100, show_eta=False, classes="xfer-bar")
        yield Static("sizing…", classes="xfer-status")

    def set_total(self, total: int) -> None:
        if total <= 0:
            self.query_one(ProgressBar).update(total=None)  # indeterminate
        self.query_one(".xfer-status", Static).update(Text("starting…", style="dim"))

    def update_progress(self, progress: Progress) -> None:
        if not self.active:
            return
        if progress.total:
            self.query_one(ProgressBar).update(total=100, progress=progress.fraction * 100)
        detail = f"{format_size(progress.done)}"
        if progress.total:
            detail += f"/{format_size(progress.total)}"
        if progress.rate:
            detail += f"  {progress.rate}"
        if progress.eta and progress.eta != "0:00:00":
            detail += f"  {progress.eta}"
        self.query_one(".xfer-status", Static).update(Text(detail, style="dim"))
        if progress.current:
            label = Text(self.label_text, no_wrap=True)
            label.append(f"  {progress.current}", style="dim italic")
            self.query_one(".xfer-label", Static).update(label)

    def finish(self, ok: bool, message: str) -> None:
        self.active = False
        bar = self.query_one(ProgressBar)
        bar.update(total=100, progress=100 if ok else bar.progress or 0)
        style = "bold #9ece6a" if ok else "bold #f7768e"
        self.query_one(".xfer-status", Static).update(Text(message, style=style))
        self.query_one(".xfer-label", Static).update(
            Text(("✓ " if ok else "✗ ") + self.label_text, style=style, no_wrap=True)
        )


class TransferPanel(VerticalScroll):
    """Holds TransferRows; hides itself while nothing has been transferred yet."""

    def on_mount(self) -> None:
        self.display = False

    def add(self, row: TransferRow) -> None:
        self.display = True
        self.mount(row)
        self.scroll_end(animate=False)

    def newest_active(self) -> TransferRow | None:
        rows = [r for r in self.query(TransferRow) if r.active]
        return rows[-1] if rows else None

    def clear_finished(self) -> None:
        rows = list(self.query(TransferRow))
        for row in rows:
            if not row.active:
                row.remove()
        # remove() is deferred, so decide from what we just saw rather than re-querying.
        if not any(row.active for row in rows):
            self.display = False


# ----------------------------------------------------------------- browser


class BrowserScreen(Screen):
    BINDINGS = [
        Binding("tab", "switch_pane", "Pane", priority=True),
        Binding("shift+tab", "switch_pane", "Pane", show=False, priority=True),
        # space/backspace/enter are spelled out in the hint bar, so keep them out of
        # the footer to stop it clipping on narrow terminals.
        Binding("space", "mark", "Mark", show=False),
        Binding("a", "mark_all", "Mark all"),
        Binding("c", "copy", "Copy →"),
        Binding("backspace", "up", "Up", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("full_stop", "hidden", "Hidden"),
        Binding("x", "cancel", "Cancel"),
        Binding("X", "clear_done", "Clear done", show=False),
        Binding("escape", "servers", "Servers"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self, server: Server):
        super().__init__()
        self.server = config.with_remembered(server)
        # Mirrored from the panes as they navigate: on unmount the panes are already
        # pruned, so the screen cannot ask them where they were.
        self.paths = {
            "local": str(Path(self.server.local).expanduser()),
            "remote": self.server.remote,
        }

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="panes"):
            yield FilePane("local", self.server, self.paths["local"], id="local")
            yield FilePane(
                "remote", self.server, self.server.remote, autoload=False, id="remote"
            )
        yield TransferPanel(id="transfers")
        yield Static(
            Text(" enter open · space mark · c copy to other pane · backspace up", style="dim"),
            id="hint",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self.server.detail
        self.local_pane.table.focus()
        self._resolve_remote()

    def on_file_pane_path_changed(self, event: FilePane.PathChanged) -> None:
        self.paths[event.side] = event.path

    def on_unmount(self) -> None:
        config.remember(self.server, self.paths["local"], self.paths["remote"])

    # ------------------------------------------------------------- helpers

    @property
    def local_pane(self) -> FilePane:
        return self.query_one("#local", FilePane)

    @property
    def remote_pane(self) -> FilePane:
        return self.query_one("#remote", FilePane)

    @property
    def focused_pane(self) -> FilePane:
        node = self.focused
        while node is not None:
            if isinstance(node, FilePane):
                return node
            node = node.parent
        return self.local_pane

    @property
    def other_pane(self) -> FilePane:
        return self.remote_pane if self.focused_pane is self.local_pane else self.local_pane

    @work(thread=True)
    def _resolve_remote(self) -> None:
        """Turn a relative/`~` remote path into an absolute one before the first listing."""
        pane = self.remote_pane
        path = pane.path
        if not path.startswith("/"):
            try:
                home = remote_home(self.server)
            except RsyncError as exc:
                self.app.call_from_thread(self._remote_failed, str(exc))
                return
            path = home if path in ("", ".", "~") else posixpath.normpath(
                posixpath.join(home, path.removeprefix("~/"))
            )
        self.app.call_from_thread(pane.go_to, path)

    def _remote_failed(self, message: str) -> None:
        pane = self.remote_pane
        pane.status = message
        pane._render_chrome()
        self.app.notify(message, title=f"Cannot reach {self.server.name}", severity="error")

    # ------------------------------------------------------------- actions

    def on_data_table_row_selected(self) -> None:
        self.focused_pane.open_current()

    def action_switch_pane(self) -> None:
        self.other_pane.table.focus()

    def action_mark(self) -> None:
        self.focused_pane.toggle_mark()

    def action_mark_all(self) -> None:
        self.focused_pane.mark_all()

    def action_up(self) -> None:
        self.focused_pane.go_up()

    def action_refresh(self) -> None:
        pane = self.focused_pane
        entry = pane.current()
        pane.reload(keep=entry.name if entry else None)

    def action_hidden(self) -> None:
        self.focused_pane.toggle_hidden()

    def action_servers(self) -> None:
        self.app.pop_screen()

    def action_quit_app(self) -> None:
        self.app.exit()

    def action_cancel(self) -> None:
        row = self.query_one(TransferPanel).newest_active()
        if row is None:
            self.notify("No transfer running", severity="warning")
        else:
            row.transfer.cancel()

    def action_clear_done(self) -> None:
        self.query_one(TransferPanel).clear_finished()

    def action_copy(self) -> None:
        source, dest = self.focused_pane, self.other_pane
        entries = source.selection()
        if not entries:
            self.notify("Nothing selected", severity="warning")
            return
        if dest.status and not dest.entries:
            self.notify(f"Destination not ready: {dest.status}", severity="error")
            return

        paths = [source.abs_path(e) for e in entries]
        name = entries[0].name if len(entries) == 1 else f"{len(entries)} items"
        arrow = "←" if source.is_remote else "→"
        transfer = Transfer(
            self.server, from_remote=source.is_remote, sources=paths, dest=dest.path
        )
        row = TransferRow(f"{arrow} {name}", transfer)
        self.query_one(TransferPanel).add(row)
        self._run_transfer(transfer, row, dest)

    @work(thread=True)
    def _run_transfer(self, transfer: Transfer, row: TransferRow, dest: FilePane) -> None:
        call = self.app.call_from_thread
        try:
            transfer.total = measure(transfer.server, transfer.from_remote, transfer.sources)
            call(row.set_total, transfer.total)

            last = 0.0

            def on_progress(progress: Progress) -> None:
                # rsync emits progress far faster than the UI needs it.
                nonlocal last
                now = time.monotonic()
                if now - last < 0.05 and progress.fraction < 1.0:
                    return
                last = now
                call(row.update_progress, replace(progress))

            transfer.run(on_progress)
        except RsyncError as exc:
            call(row.finish, False, str(exc))
            call(self.app.notify, str(exc), title="Transfer failed", severity="error")
            return
        if transfer.cancelled:
            call(row.finish, False, "cancelled")
            return
        call(row.finish, True, "done")
        call(dest.reload)


# ------------------------------------------------------------ server list


class AddServerScreen(ModalScreen[Server | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    FIELDS = [
        ("name", "Name", "how it shows in the list"),
        ("host", "Host", "leave blank to use Name as the hostname"),
        ("user", "User", "optional"),
        ("port", "Port", "optional"),
        ("key", "Identity file", "optional, e.g. ~/.ssh/id_ed25519"),
        ("remote", "Remote path", "."),
        ("local", "Local path", "~"),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label("Add server", id="dialog-title")
            for key, label, placeholder in self.FIELDS:
                with Horizontal(classes="field-row"):
                    yield Label(label, classes="field-label")
                    yield Input(placeholder=placeholder, id=f"field-{key}")
            with Horizontal(id="dialog-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#field-name", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        self._save()

    def on_input_submitted(self) -> None:
        self._save()

    def _save(self) -> None:
        values = {
            key: self.query_one(f"#field-{key}", Input).value.strip()
            for key, _, _ in self.FIELDS
        }
        if not values["name"]:
            self.notify("Name is required", severity="error")
            self.query_one("#field-name", Input).focus()
            return
        if values["port"] and not values["port"].isdigit():
            self.notify("Port must be a number", severity="error")
            return
        self.dismiss(
            Server(
                name=values["name"],
                host=values["host"] or None,
                user=values["user"] or None,
                port=int(values["port"]) if values["port"] else None,
                key=values["key"] or None,
                remote=values["remote"] or ".",
                local=values["local"] or "~",
                source="snag",
            )
        )


class ServerScreen(Screen):
    BINDINGS = [
        Binding("a", "add", "Add"),
        Binding("d", "delete", "Remove"),
        Binding("r", "reload", "Reload"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.servers: list[Server] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(Text(" Pick a server — enter to connect", style="dim"), id="hint")
        yield DataTable(cursor_type="row", zebra_stripes=True, id="servers")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "servers"
        table = self.query_one("#servers", DataTable)
        table.add_column("Name", width=28)
        table.add_column("Connection", width=34)
        table.add_column("Remote path")
        table.add_column("From", width=10)
        self.action_reload()
        table.focus()

    def action_reload(self) -> None:
        self.servers = config.load_servers()
        table = self.query_one("#servers", DataTable)
        table.clear()
        for server in self.servers:
            saved = config.with_remembered(server)
            table.add_row(
                Text(server.name, style="bold"),
                Text(server.detail, style="dim"),
                Text(saved.remote, style="#7dcfff"),
                Text("ssh config" if server.source == "ssh" else "snag", style="dim italic"),
            )
        if not self.servers:
            self.notify("No servers found — press 'a' to add one", severity="warning")

    def _current(self) -> Server | None:
        row = self.query_one("#servers", DataTable).cursor_row
        return self.servers[row] if 0 <= row < len(self.servers) else None

    def on_data_table_row_selected(self) -> None:
        server = self._current()
        if server:
            self.app.push_screen(BrowserScreen(server))

    def action_add(self) -> None:
        def saved(server: Server | None) -> None:
            if server is None:
                return
            own = [s for s in config.load_servers() if s.source == "snag" and s.name != server.name]
            config.save_servers([*own, server])
            self.action_reload()
            self.notify(f"Saved {server.name}")

        self.app.push_screen(AddServerScreen(), saved)

    def action_delete(self) -> None:
        server = self._current()
        if server is None:
            return
        if server.source != "snag":
            self.notify(
                "That host comes from ~/.ssh/config — remove it there", severity="warning"
            )
            return
        config.save_servers([s for s in self.servers if s.source == "snag" and s is not server])
        self.action_reload()
        self.notify(f"Removed {server.name}")

    def action_quit_app(self) -> None:
        self.app.exit()


class SnagApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "snag"

    def on_mount(self) -> None:
        self.theme = "tokyo-night"
        self.push_screen(ServerScreen())
