"""Locks for one owner's write command in an existing READ COMMITTED transaction."""

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from financeiq.db.models import metadata


async def lock_financial_owner(session: AsyncSession, owner_id: UUID) -> None:
    """Acquire the root in its own statement before reading the mutable plan."""
    if not session.in_transaction():
        raise RuntimeError("Financial locking requires an active transaction.")
    await session.execute(
        text("SELECT public.lock_financial_owner(:owner_id)"), {"owner_id": owner_id}
    )


async def lock_financial_rows(
    session: AsyncSession,
    owner_id: UUID,
    *,
    account_ids: Collection[UUID] = (),
    batch_ids: Collection[UUID] = (),
    attempt_ids: Collection[UUID] = (),
    category_ids: Collection[UUID] = (),
    rule_ids: Collection[UUID] = (),
    transaction_ids: Collection[UUID] = (),
    transfer_link_ids: Collection[UUID] = (),
) -> None:
    """Lock the targets discovered after taking this owner's root and rereading."""
    await lock_financial_owner(session, owner_id)
    for table_name, requested in (
        ("accounts", account_ids),
        ("import_batches", batch_ids),
        ("import_attempts", attempt_ids),
        ("categories", category_ids),
        ("categorization_rules", rule_ids),
        ("transactions", transaction_ids),
        ("transfer_links", transfer_link_ids),
    ):
        if not requested:
            continue
        ids = sorted(set(requested))
        if table_name == "import_batches":
            locked = await session.scalar(
                text(
                    "SELECT public.lock_financial_import_batches("
                    ":owner_id, CAST(:ids AS uuid[]))"
                ),
                {"owner_id": owner_id, "ids": ids},
            )
        else:
            table = metadata.tables[table_name]
            rows = await session.scalars(
                select(table.c.id)
                .where(table.c.user_id == owner_id, table.c.id.in_(ids))
                .order_by(table.c.id)
                .with_for_update(key_share=True)
            )
            locked = len(rows.all())
        if locked != len(ids):
            raise LookupError("Financial lock target does not exist.")
