"""Server definitions: snag's own TOML config plus hosts imported from ~/.ssh/config."""

from __future__ import annotations

import json
import os
import re
import shlex
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "snag"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "snag"
SERVERS_FILE = CONFIG_DIR / "servers.toml"
STATE_FILE = STATE_DIR / "state.json"

SSH_CONFIG = Path.home() / ".ssh" / "config"


@dataclass(frozen=True)
class Server:
    """One remote. `source` records where the definition came from."""

    name: str
    host: str | None = None
    user: str | None = None
    port: int | None = None
    key: str | None = None
    remote: str = "."
    local: str = "~"
    source: str = "snag"  # "snag" | "ssh"

    @property
    def target(self) -> str:
        """The `[user@]host` half of an rsync remote spec."""
        host = self.host or self.name
        return f"{self.user}@{host}" if self.user else host

    @property
    def ssh_command(self) -> list[str]:
        """ssh invocation honouring any port/key we hold that ssh_config would not."""
        cmd = ["ssh"]
        if self.port:
            cmd += ["-p", str(self.port)]
        if self.key:
            cmd += ["-i", os.path.expanduser(self.key)]
        return cmd

    @property
    def rsync_shell(self) -> list[str]:
        """`-e ...` args for rsync, or [] when plain ssh resolution suffices."""
        cmd = self.ssh_command
        return [] if cmd == ["ssh"] else ["-e", shlex.join(cmd)]

    @property
    def detail(self) -> str:
        bits = self.host or self.name
        if self.user:
            bits = f"{self.user}@{bits}"
        if self.port:
            bits = f"{bits}:{self.port}"
        return bits


# ---------------------------------------------------------------- ssh config

_TOKEN = re.compile(r"^\s*(\w+)[\s=]+(.+?)\s*$")


def parse_ssh_config(path: Path = SSH_CONFIG, _seen: set[Path] | None = None) -> list[Server]:
    """Pull Host blocks out of an ssh config, following Include directives.

    Patterns containing wildcards are skipped: they are defaults, not connectable hosts.
    """
    _seen = _seen if _seen is not None else set()
    path = path.expanduser()
    try:
        resolved = path.resolve()
    except OSError:
        return []
    if resolved in _seen or not path.is_file():
        return []
    _seen.add(resolved)

    servers: list[Server] = []
    names: list[str] = []
    fields: dict[str, str] = {}

    def flush() -> None:
        for name in names:
            servers.append(
                Server(
                    name=name,
                    host=fields.get("hostname"),
                    user=fields.get("user"),
                    port=int(fields["port"]) if fields.get("port", "").isdigit() else None,
                    key=fields.get("identityfile"),
                    source="ssh",
                )
            )

    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []

    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _TOKEN.match(line)
        if not m:
            continue
        keyword, value = m.group(1).lower(), m.group(2)

        if keyword == "include":
            flush()
            names, fields = [], {}
            for pattern in shlex.split(value):
                base = Path(pattern).expanduser()
                if not base.is_absolute():
                    base = SSH_CONFIG.parent / pattern
                for inc in sorted(base.parent.glob(base.name)):
                    servers.extend(parse_ssh_config(inc, _seen))
        elif keyword == "host":
            flush()
            names = [n for n in shlex.split(value) if not set(n) & {"*", "?", "!"}]
            fields = {}
        elif keyword == "match":
            flush()
            names, fields = [], {}
        elif names:
            fields.setdefault(keyword, value)

    flush()
    return servers


# ------------------------------------------------------------- snag servers


def _load_toml() -> list[Server]:
    if not SERVERS_FILE.is_file():
        return []
    try:
        data = tomllib.loads(SERVERS_FILE.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    out = []
    for raw in data.get("server", []):
        if not raw.get("name"):
            continue
        out.append(
            Server(
                name=str(raw["name"]),
                host=raw.get("host"),
                user=raw.get("user"),
                port=int(raw["port"]) if raw.get("port") else None,
                key=raw.get("key"),
                remote=raw.get("remote", "."),
                local=raw.get("local", "~"),
                source="snag",
            )
        )
    return out


def _toml_escape(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_servers(servers: list[Server]) -> None:
    """Persist only snag-owned servers; ssh_config hosts stay in ssh_config."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    chunks = [
        "# snag servers. Hosts in ~/.ssh/config are picked up automatically;",
        "# add an entry here only to define extra hosts or override defaults.\n",
    ]
    for s in servers:
        if s.source != "snag":
            continue
        chunks.append("[[server]]")
        chunks.append(f"name = {_toml_escape(s.name)}")
        for field, value in (("host", s.host), ("user", s.user), ("key", s.key)):
            if value:
                chunks.append(f"{field} = {_toml_escape(value)}")
        if s.port:
            chunks.append(f"port = {s.port}")
        chunks.append(f"remote = {_toml_escape(s.remote)}")
        chunks.append(f"local = {_toml_escape(s.local)}\n")
    SERVERS_FILE.write_text("\n".join(chunks))


def load_servers() -> list[Server]:
    """snag's own servers first, then ssh_config hosts that snag does not already name.

    A snag entry that repeats an ssh_config name is treated as an override of it, so
    remembered paths and explicit ports win while ssh still resolves the connection.
    """
    own = _load_toml()
    claimed = {s.name for s in own}
    merged = list(own)
    for s in parse_ssh_config():
        if s.name not in claimed:
            claimed.add(s.name)
            merged.append(s)
    return merged


# --------------------------------------------------------------------- state


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def remember(server: Server, local: str, remote: str) -> None:
    """Record the directories a server was last browsing, so the next visit resumes there."""
    state = load_state()
    state.setdefault("servers", {})[server.name] = {"local": local, "remote": remote}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def _first_existing_dir(*candidates: str | None) -> str:
    """First candidate that is still a directory, else the home directory.

    A remembered path can go stale between sessions (the directory gets deleted, or an
    external volume is not mounted), and a pane opening on a dead path is just broken.
    """
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_dir():
            return str(path)
    return str(Path.home())


def with_remembered(server: Server) -> Server:
    """Apply any remembered directories on top of a server's configured defaults."""
    saved = load_state().get("servers", {}).get(server.name, {})
    return replace(
        server,
        # The remote is only reachable over the network, so a stale one is reported by
        # the pane instead; the local side we can check up front.
        local=_first_existing_dir(saved.get("local"), server.local),
        remote=saved.get("remote") or server.remote,
    )
