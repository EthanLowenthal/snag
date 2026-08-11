"""Server definitions: what snag imports from ssh_config, and remembered directories."""

from __future__ import annotations

import pytest

from snag import config as cfg_mod
from snag.config import Server, parse_ssh_config, save_servers, with_remembered


@pytest.fixture
def ssh_servers(tmp_path) -> dict[str, Server]:
    """An ssh config with a wildcard block, a multi-name Host, and an Include."""
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "more").write_text("Host imported\n  HostName 10.9.9.9\n")
    cfg = tmp_path / "ssh_config"
    cfg.write_text(
        "Host *\n  ForwardAgent yes\n"
        "Host alpha\n  HostName alpha.example.com\n  User ada\n  Port 2222\n"
        "Host beta gamma\n  HostName shared.example.com\n"
        f"Include {extra / 'more'}\n"
    )
    return {s.name: s for s in parse_ssh_config(cfg)}


def test_wildcard_host_skipped(ssh_servers):
    assert "*" not in ssh_servers, f"parsed {sorted(ssh_servers)}"


def test_fields_parsed(ssh_servers):
    assert ssh_servers["alpha"].detail == "ada@alpha.example.com:2222"


def test_multi_name_host_expands(ssh_servers):
    assert {"beta", "gamma"} <= ssh_servers.keys(), f"parsed {sorted(ssh_servers)}"


def test_include_followed(ssh_servers):
    assert "imported" in ssh_servers, f"parsed {sorted(ssh_servers)}"


def test_all_marked_as_ssh_sourced(ssh_servers):
    assert all(s.source == "ssh" for s in ssh_servers.values())


@pytest.fixture
def resolved(tmp_path) -> Server:
    """A server whose remembered local directory has gone away since it was recorded."""
    server = Server(name="ghost", local=str(tmp_path), remote="/data")
    save_servers([server])
    cfg_mod.remember(server, "/definitely-not-here-xyz", "/data/runs")
    return with_remembered(server)


def test_dead_local_path_ignored(resolved, tmp_path):
    assert resolved.local == str(tmp_path)


def test_remote_path_still_remembered(resolved):
    assert resolved.remote == "/data/runs"
