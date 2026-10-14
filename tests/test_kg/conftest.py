from datetime import datetime, timedelta, timezone

import pytest

from src.kg.store import KGStore


class FakeClock:
    """Controllable UTC clock so TTL behaviour is deterministic."""

    def __init__(self, start: datetime | None = None):
        self.current = start or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs) -> None:
        self.current += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock) -> KGStore:
    return KGStore(tmp_path / "kg_test.db", now_fn=clock)

