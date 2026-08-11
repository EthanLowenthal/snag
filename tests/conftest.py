"""Shared setup. No network and no mocking of rsync itself: the "remote" side of every
command is a local path, so real rsync processes and the real Textual UI are exercised
while everything stays on disk.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# snag.config reads these at import time, so they have to be set before the first import
# of anything under snag. conftest is imported ahead of the test modules, so this is it.
_SCRATCH = Path(tempfile.mkdtemp(prefix="snag-tests-"))
os.environ["XDG_CONFIG_HOME"] = str(_SCRATCH / "config")
os.environ["XDG_STATE_HOME"] = str(_SCRATCH / "state")

import pytest  # noqa: E402

from snag import app as app_mod  # noqa: E402
from snag import config as cfg_mod  # noqa: E402
from snag import rsync as rsync_mod  # noqa: E402
from snag.config import Server, save_servers  # noqa: E402
from snag.rsync import local_list  # noqa: E402

from helpers import make_tree  # noqa: E402


@pytest.fixture(autouse=True)
def private_config(tmp_path, monkeypatch):
    """Config and state per test, so the directories one test remembers do not leak."""
    config_dir, state_dir = tmp_path / "config" / "snag", tmp_path / "state" / "snag"
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "SERVERS_FILE", config_dir / "servers.toml")
    monkeypatch.setattr(cfg_mod, "STATE_DIR", state_dir)
    monkeypatch.setattr(cfg_mod, "STATE_FILE", state_dir / "state.json")


@pytest.fixture(autouse=True)
def local_remote(monkeypatch):
    """Make every "remote" path a local path, and list it without going through ssh."""
    monkeypatch.setattr(rsync_mod, "_remote_spec", lambda server, path: path)
    monkeypatch.setattr(app_mod, "remote_list", lambda server, path: local_list(Path(path)))


@pytest.fixture
def tree(tmp_path):
    """Factory for a local/remote pair; `big` sizes the file worth watching transfer."""
    return lambda big=20_000_000: make_tree(tmp_path, big)


@pytest.fixture
def remote_server(tmp_path, monkeypatch):
    """Factory for a tree that snag knows as its one and only server."""

    def build(name: str, big: int = 20_000_000) -> tuple[Path, Path]:
        local, remote = make_tree(tmp_path, big)
        monkeypatch.setattr(app_mod, "remote_home", lambda server: str(remote))
        # The real ~/.ssh/config would otherwise add hosts to the server list.
        monkeypatch.setattr(cfg_mod, "parse_ssh_config", lambda *a, **kw: [])
        save_servers([Server(name=name, host="localhost", remote=".", local=str(local))])
        return local, remote

    return build
