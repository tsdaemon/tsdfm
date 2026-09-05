import pytest

from tsdfm import state


@pytest.fixture(autouse=True)
def fast_timings(monkeypatch):
    """The sync loop's real cadence would make tests sleep for seconds at a time."""
    monkeypatch.setattr(state, "SYNC_INTERVAL", 0.01)
