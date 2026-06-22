"""Test isolation: redirect persisted state/journal to a per-test tmp dir so
SessionRisk persistence and the journal never touch the real state/ tree."""

import pytest

from winthorpe import config


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "JOURNAL_DIR", tmp_path / "journal")
    (tmp_path / "state").mkdir()
    (tmp_path / "journal").mkdir()
    yield
