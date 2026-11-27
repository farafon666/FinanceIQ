"""Password hashing and verification guarantees shared by the Auth commands."""

import asyncio
from threading import Event, get_ident

import pytest
from argon2 import PasswordHasher

from financeiq.modules.auth import passwords


@pytest.mark.asyncio
async def test_hash_password_uses_a_fresh_salt() -> None:
    password = "Synthetic password for salt checks"
    first = await passwords.hash_password(password)
    second = await passwords.hash_password(password)

    assert first.split("$")[-2] != second.split("$")[-2]
    verifier = PasswordHasher()
    assert verifier.verify(first, password)
    assert verifier.verify(second, password)


@pytest.mark.asyncio
async def test_hashing_and_verification_share_two_worker_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    event_loop_thread = get_ident()
    started: asyncio.Queue[int] = asyncio.Queue()
    release = Event()

    def block_worker() -> None:
        loop.call_soon_threadsafe(started.put_nowait, get_ident())
        assert release.wait(timeout=5)

    class ControlledHasher:
        def hash(self, password: str) -> str:
            block_worker()
            return password

        def verify(self, encoded: str, password: str) -> bool:
            block_worker()
            return True

        def check_needs_rehash(self, encoded: str) -> bool:
            return False

    monkeypatch.setattr(passwords, "_hasher", ControlledHasher())
    first = asyncio.create_task(passwords.hash_password("first"))
    second = asyncio.create_task(passwords.verify_password("second", "synthetic hash"))
    third = asyncio.create_task(passwords.hash_password("third"))
    try:
        first_worker = await asyncio.wait_for(started.get(), timeout=3)
        second_worker = await asyncio.wait_for(started.get(), timeout=3)
        worker_threads = {first_worker, second_worker}
        assert len(worker_threads) == 2 and event_loop_thread not in worker_threads
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(started.get(), timeout=0.2)
    finally:
        release.set()
        hashed, verified, queued = await asyncio.gather(
            first, second, third, return_exceptions=True
        )

    assert hashed == "first"
    assert verified == (True, None)
    assert queued == "third"
    assert started.get_nowait() in worker_threads
