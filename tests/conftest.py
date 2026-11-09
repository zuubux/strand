"""Shared fixtures: the resolver's host wall clock is frozen so envelopes are deterministic."""

from datetime import datetime, timezone

import pytest

HOST_TIME_LINE = "[Host Temporal Context: Thursday, January 02, 2031 03:04:05 UTC]"


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2031, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    def astimezone(self, tz=None):
        return self if tz is None else super().astimezone(tz)


@pytest.fixture(autouse=True)
def frozen_host_clock(monkeypatch):
    monkeypatch.setattr("strand.resolver.datetime", _FrozenDatetime)
