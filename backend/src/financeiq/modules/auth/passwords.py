"""Argon2id work runs outside the event loop with a shared process limit."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1, type=Type.ID)
# Generated with the same parameters and a discarded random password.
DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=19456,t=2,p=1$tv5pg6ajcOoca5XinVfs4w$"
    "rrkMDRrzt5+RCP6K/qBHRJi6cxovEudNA4yHaVQ8T0E"
)
# The executor starts workers on demand and keeps the limit across event loops.
_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="financeiq-password")


async def hash_password(password: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(
        _workers, _hasher.hash, password
    )


async def verify_password(
    password: str, password_hash: str | None
) -> tuple[bool, str | None]:
    return await asyncio.get_running_loop().run_in_executor(
        _workers, _verify_password, password, password_hash
    )


def _verify_password(
    password: str, password_hash: str | None
) -> tuple[bool, str | None]:
    try:
        _hasher.verify(
            DUMMY_PASSWORD_HASH if password_hash is None else password_hash, password
        )
    except VerifyMismatchError:
        return False, None
    if password_hash is None:
        return False, None
    replacement = (
        _hasher.hash(password) if _hasher.check_needs_rehash(password_hash) else None
    )
    return True, replacement
