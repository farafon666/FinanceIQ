"""The storage evidence envelope is deliberately smaller than service payloads."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from financeiq.shared.financial_evidence import ActionEvidence, TransactionState


def test_evidence_preserves_service_parameters_and_explicit_nulls() -> None:
    owner = uuid4()
    evidence = ActionEvidence.model_validate(
        {
            "accepted_plan": {
                "user_id": str(owner),
                "action_type": "confirm_balance",
                "account_id": str(uuid4()),
                "source_attempt_id": None,
                "consents": ["eod_balance"],
                "parameters": {"balance": "-0.01", "balance_date": "2026-10-01"},
            },
            "result": {"confirmation_id": str(uuid4())},
        }
    )
    value = evidence.model_dump(mode="json")
    assert value["accepted_plan"]["user_id"] == str(owner)
    assert value["accepted_plan"]["parameters"]["balance"] == "-0.01"
    assert value["accepted_plan"]["source_attempt_id"] is None


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"accepted_plan": {}, "result": {}},
        {"accepted_plan": [], "result": {}},
        {
            "accepted_plan": {"user_id": str(uuid4()), "action_type": "unknown"},
            "result": {},
        },
    ],
)
def test_incomplete_evidence_is_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        ActionEvidence.model_validate(value)


@pytest.mark.parametrize("number", [0.1, float("nan"), float("inf")])
def test_binary_float_cannot_enter_nested_plan_or_result(number: float) -> None:
    with pytest.raises(ValidationError):
        ActionEvidence.model_validate(
            {
                "accepted_plan": {
                    "user_id": str(uuid4()),
                    "action_type": "set_kind",
                    "account_id": None,
                    "source_attempt_id": None,
                    "consents": [],
                    "parameters": {"nested": [number]},
                },
                "result": {},
            }
        )


def test_transaction_snapshot_requires_revision_and_explicit_nullable_fields() -> None:
    with pytest.raises(ValidationError):
        TransactionState.model_validate({"kind": "unclassified", "revision": 1})
