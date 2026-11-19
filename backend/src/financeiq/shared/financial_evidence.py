"""Version-one storage evidence, independent of HTTP and financial services.

Command-specific parameters/results remain extensible JSON objects. Database
guards check their required subset against actual changes; this envelope is not
authorization, nor proof that a person reviewed the source statement.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

ActionType = Literal[
    "import_confirm",
    "import_cancel",
    "set_kind",
    "set_category",
    "confirm_transfer",
    "replace_transfer",
    "unlink_transfer",
    "link_refund",
    "unlink_refund",
    "confirm_unlinked_refund",
    "confirm_balance",
    "revoke_balance",
    "confirm_history",
    "revoke_history",
    "archive_account",
    "restore_account",
]
TransactionKind = Literal["income", "expense", "transfer", "refund", "unclassified"]


def _without_float(value: JsonValue) -> JsonValue:
    if isinstance(value, float):
        raise ValueError("Financial evidence cannot contain binary float")
    if isinstance(value, dict):
        for child in value.values():
            _without_float(child)
    elif isinstance(value, list):
        for child in value:
            _without_float(child)
    return value


class EvidencePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    action_type: ActionType
    account_id: UUID | None
    source_attempt_id: UUID | None
    consents: list[str]
    parameters: dict[str, JsonValue]

    @field_validator("parameters")
    @classmethod
    def exact_parameters(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _without_float(value)
        return value


class ActionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted_plan: EvidencePlan
    result: dict[str, JsonValue]

    @field_validator("result")
    @classmethod
    def exact_result(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _without_float(value)
        return value


class TransactionState(BaseModel):
    """The mutable projection compared with each actual OLD/NEW row image."""

    model_config = ConfigDict(extra="forbid")

    kind: TransactionKind
    category_id: UUID | None
    category_origin: Literal["none", "manual", "rule"]
    categorization_rule_id: UUID | None
    refund_of_transaction_id: UUID | None
    refund_action_id: UUID | None
    last_action_id: UUID
    revision: int = Field(gt=0)
    cancelled_by_id: UUID | None
