"""The transfer engine, driven with real rsync processes."""

from __future__ import annotations

import pytest

from snag import rsync as rsync_mod
from snag.config import Server
from snag.rsync import RsyncError, measure

from helpers import Throttled


def test_transfer_engine(tree):
    local, remote = tree(big=40_000_000)
    server = Server(name="fake")
    sources = [str(remote / "capture.raw"), str(remote / "logs"), str(remote / "summary.json")]

    expected = 40_000_000 + 13 + len("line\n") * 200
    total = measure(server, True, sources)
    assert total == expected, f"measure() summed {total:,}, selection holds {expected:,}"

    samples: list[tuple[int, str]] = []
    transfer = Throttled(server, from_remote=True, sources=sources, dest=str(local), total=total)
    transfer.run(lambda p: samples.append((p.done, p.eta)))

    dones = [d for d, _ in samples]
    assert all(a <= b for a, b in zip(dones, dones[1:])), f"progress went backwards: {dones}"
    assert dones[-1] == total, f"progress stopped at {dones[-1]:,}/{total:,}"
    assert len(samples) > 5, f"only {len(samples)} progress callbacks"

    etas = [e for _, e in samples if e]
    assert etas == sorted(etas, reverse=True), f"overall ETA does not decrease: {etas[:4]}…"

    on_disk = sum(f.stat().st_size for f in local.rglob("*") if f.is_file())
    assert on_disk == total, f"{on_disk:,} bytes landed of {total:,}"
    assert (local / "logs" / "run.log").exists(), "directory source was not recursed"


def test_unreachable_host_error(monkeypatch):
    """The message has to name the ssh failure, not rsync's generic wrapper."""
    monkeypatch.setattr(rsync_mod, "_remote_spec", lambda s, p: f"{s.target}:{p}")
    dead = Server(name="dead", host="127.0.0.1", port=1)

    with pytest.raises(RsyncError) as caught:
        rsync_mod.remote_list(dead, "/data")

    message = str(caught.value)
    assert "connect to host" in message, message
    assert "unexpected end of file" not in message, message
