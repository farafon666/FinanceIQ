"""Technical ORM records only: no domain models or application metadata."""

from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, Numeric
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ProbeBase(DeclarativeBase):
    pass


class ProbeParent(ProbeBase):
    __tablename__ = "test_runtime_parent"
    id: Mapped[UUID] = mapped_column(primary_key=True)


class ProbeEntry(ProbeBase):
    __tablename__ = "test_runtime_entry"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    parent_id: Mapped[UUID] = mapped_column(
        ForeignKey("test_runtime_parent.id", deferrable=True, initially="DEFERRED")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    note: Mapped[str]


def build_parent(*, id: UUID | None = None) -> ProbeParent:
    return ProbeParent(id=id if id is not None else uuid4())


def build_entry(
    parent_id: UUID,
    *,
    id: UUID | None = None,
    amount: Decimal = Decimal("12.34"),
    note: str = "synthetic-record",
) -> ProbeEntry:
    return ProbeEntry(
        id=id if id is not None else uuid4(),
        parent_id=parent_id,
        amount=amount,
        note=note,
    )
