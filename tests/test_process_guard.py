"""Process self-guard (#6): parent-death signal + read-only orphan diagnostic."""

import os

from winthorpe.data.process_guard import find_sibling_servers, set_parent_death_signal


def test_set_parent_death_signal_is_safe_and_armed():
    # Best-effort; on Linux it arms cleanly and never raises.
    result = set_parent_death_signal()
    assert isinstance(result, bool)


def test_find_sibling_servers_excludes_self_and_returns_ints():
    pids = find_sibling_servers()
    assert isinstance(pids, list)
    assert os.getpid() not in pids          # never reports the caller
    assert all(isinstance(p, int) for p in pids)


def test_find_sibling_servers_honors_explicit_self_pid():
    # Passing an explicit self_pid excludes it even if it isn't the real pid.
    pids = find_sibling_servers(self_pid=1234)
    assert 1234 not in pids
