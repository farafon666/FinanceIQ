"""Keep unit HTTP tests independent of the maintenance database connection."""

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def stub_auth_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("financeiq.main.run_auth_cleanup", AsyncMock())
