"""Explicit registry of domain mappings for Alembic and schema inspection."""

from financeiq.db.base import Base
from financeiq.db.financial_actions import FinancialAction
from financeiq.modules.accounts.models import (
    Account,
    BalanceConfirmation,
    HistoryConfirmation,
)
from financeiq.modules.auth.models import AuthSession, User

__all__ = [
    "Account",
    "AuthSession",
    "BalanceConfirmation",
    "FinancialAction",
    "HistoryConfirmation",
    "User",
    "metadata",
]

metadata = Base.metadata
