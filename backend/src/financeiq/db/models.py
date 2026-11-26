"""Explicit registry of domain mappings for Alembic and schema inspection."""

from financeiq.db.base import Base
from financeiq.db.financial_actions import FinancialAction, FinancialActionTransaction
from financeiq.modules.accounts.models import (
    Account,
    BalanceConfirmation,
    HistoryConfirmation,
)
from financeiq.modules.auth.models import AuthRateLimit, AuthSession, User
from financeiq.modules.categories.models import CategorizationRule, Category
from financeiq.modules.imports.models import (
    ImportAccountMapping,
    ImportAttempt,
    ImportBatch,
    ImportCancellation,
    ImportMovement,
    ImportRow,
)
from financeiq.modules.transactions.models import Transaction, TransferLink

__all__ = [
    "Account",
    "AuthRateLimit",
    "AuthSession",
    "BalanceConfirmation",
    "CategorizationRule",
    "Category",
    "FinancialAction",
    "FinancialActionTransaction",
    "HistoryConfirmation",
    "ImportAccountMapping",
    "ImportAttempt",
    "ImportBatch",
    "ImportCancellation",
    "ImportMovement",
    "ImportRow",
    "Transaction",
    "TransferLink",
    "User",
    "metadata",
]

metadata = Base.metadata
