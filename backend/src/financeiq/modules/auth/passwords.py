"""Argon2id work runs outside the event loop with a shared process limit."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from argon2 import PasswordHasher, Type

_hasher = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1, type=Type.ID)
# The executor starts workers on demand and keeps the limit across event loops.
_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="financeiq-password")


async def hash_password(password: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(
        _workers, _hasher.hash, password
    )
