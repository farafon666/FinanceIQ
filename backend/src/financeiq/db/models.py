"""Explicit registry of domain mappings for Alembic and schema inspection."""

from financeiq.db.base import Base
from financeiq.db.financial_actions import FinancialAction
from financeiq.modules.accounts.models import (
    Account,
    BalanceConfirmation,
    HistoryConfirmation,
)
from financeiq.modules.auth.models import AuthSession, User
from financeiq.modules.categories.models import CategorizationRule, Category
from financeiq.modules.imports.models import (
    ImportAccountMapping,
    ImportAttempt,
    ImportBatch,
    ImportCancellation,
    ImportMovement,
    ImportRow,
)

__all__ = [
    "Account",
    "AuthSession",
    "BalanceConfirmation",
    "CategorizationRule",
    "Category",
    "FinancialAction",
    "HistoryConfirmation",
    "ImportAccountMapping",
    "ImportAttempt",
    "ImportBatch",
    "ImportCancellation",
    "ImportMovement",
    "ImportRow",
    "User",
    "metadata",
]

metadata = Base.metadata
