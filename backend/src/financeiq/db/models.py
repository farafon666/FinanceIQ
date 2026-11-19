"""Explicit registry of domain mappings for Alembic and schema inspection."""

from financeiq.db.base import Base
from financeiq.modules.auth.models import AuthSession, User

__all__ = ["AuthSession", "User", "metadata"]

metadata = Base.metadata
