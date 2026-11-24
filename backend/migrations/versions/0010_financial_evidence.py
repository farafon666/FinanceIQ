"""Require transfer, refund and import evidence to match the recorded changes."""

from pathlib import Path
from runpy import run_path
from typing import cast

from alembic import op

revision: str = "0010"
down_revision: str = "0009"
branch_labels: str | None = None
depends_on: str | None = None

FUNCTION_CHANGES = {
    "financial_check_transfer": (
        """    IF NOT (a.details->'accepted_plan'->'consents' @> '["own_accounts"]')
        OR NOT (a.details->'accepted_plan'->'parameters'->'transfer_links' @>
            jsonb_build_array(jsonb_build_object('id',l.id,'outgoing_transaction_id',
                l.outgoing_transaction_id,'incoming_transaction_id',l.incoming_transaction_id))) THEN""",
        """    IF (a.details->'accepted_plan'->'consents' @> '["own_accounts"]') IS NOT TRUE
        OR jsonb_typeof(a.details->'accepted_plan'->'parameters'->'transfer_links') IS DISTINCT FROM 'array'
        OR (a.details->'accepted_plan'->'parameters'->'transfer_links' @>
            jsonb_build_array(jsonb_build_object('id',l.id,'outgoing_transaction_id',
                l.outgoing_transaction_id,'incoming_transaction_id',l.incoming_transaction_id))) IS NOT TRUE THEN""",
    ),
    "financial_check_transaction": (
        """        IF t.refund_of_transaction_id IS NULL THEN
            IF NOT (a.details->'accepted_plan'->'consents' @> '["unlinked_refund","unverified_limit"]') THEN
                RAISE EXCEPTION 'Unlinked refund requires explicit acknowledgement' USING ERRCODE='23514';
            END IF;
        ELSE
            SELECT * INTO original""",
        """        IF t.refund_of_transaction_id IS NOT NULL THEN
            SELECT * INTO original""",
    ),
    "financial_check_action": (
        "    IF a.action_type='import_confirm' THEN\n",
        """    -- Check the establishing snapshot even when the refund has since been
    -- cancelled or reclassified. A later audit item can preserve its evidence.
    IF EXISTS(SELECT 1 FROM public.financial_action_transactions i
        WHERE i.user_id=owner AND i.action_id=a.id AND i.after_state->>'kind'='refund'
            AND i.after_state->'refund_action_id'=to_jsonb(a.id)
            AND CASE WHEN i.after_state->'refund_of_transaction_id'='null'::jsonb THEN
                a.action_type NOT IN ('confirm_unlinked_refund','import_confirm')
                OR (a.details->'accepted_plan'->'consents' @>
                    '["unlinked_refund","unverified_limit"]') IS NOT TRUE
            ELSE a.action_type NOT IN ('link_refund','import_confirm') END) THEN
        RAISE EXCEPTION 'Refund action and acknowledgements must match the original link' USING ERRCODE='23514';
    END IF;
    IF a.action_type='import_confirm' THEN
""",
    ),
    "financial_accept_guard": (
        """BEGIN
    -- Deferred final-state checks permit historical observations only after a
    -- later cancellation. They must not allow a new observation to be born
    -- cancelled and hidden by cancelling its own result in the same xact.
    IF NEW.status='accepted' AND OLD.status<>'accepted' AND EXISTS(""",
        """DECLARE classifications jsonb;
BEGIN
    IF NEW.status IS DISTINCT FROM 'accepted' OR OLD.status='accepted' THEN RETURN NEW; END IF;
    -- Deferred checks cannot distinguish stale evidence from a change made
    -- after acceptance, even within the same transaction.
    classifications:=(public.financial_action(NEW.user_id,NEW.accepted_action_id,
        ARRAY['import_confirm'])).details->'result'->'classifications';
    IF jsonb_typeof(classifications) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'Accepted result must retain original classifications' USING ERRCODE='23514';
    END IF;
    IF EXISTS(SELECT 1 FROM public.import_movements m
        LEFT JOIN public.transactions t ON t.user_id=m.user_id AND t.id=m.result_transaction_id
        LEFT JOIN jsonb_array_elements(classifications) saved(value)
            ON saved.value->'transaction_id'=to_jsonb(m.result_transaction_id)
        WHERE m.user_id=NEW.user_id AND m.attempt_id=NEW.id
            AND (t.id IS NULL OR saved.value->'state' IS DISTINCT FROM public.financial_state(to_jsonb(t)))) THEN
        RAISE EXCEPTION 'Saved classifications must match the facts at acceptance' USING ERRCODE='23514';
    END IF;
    -- A new link must be active before any later cancellation in this xact.
    IF EXISTS(""",
    ),
}


def _replace_functions(*, upgrade: bool) -> None:
    # These functions have not changed since 0007. Use that immutable revision
    # as the source for the unchanged SQL and the exact downgrade definitions.
    previous = run_path(str(Path(__file__).with_name("0007_financial_constraints.py")))
    functions = cast("dict[str, tuple[str, str]]", previous["FUNCTIONS"])
    for name, (old, new) in FUNCTION_CHANGES.items():
        arguments, body = functions[name]
        if upgrade:
            if body.count(old) != 1:
                raise RuntimeError(f"Unexpected historical function definition: {name}")
            body = body.replace(old, new, 1)
        body = body.replace(
            " AS $$", " SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$", 1
        )
        op.execute(f"CREATE OR REPLACE FUNCTION public.{name}({arguments}) {body}")


def _lock_tables() -> None:
    previous = run_path(
        str(Path(__file__).with_name("0008_financial_owner_locking.py"))
    )
    tables = cast("tuple[str, ...]", previous["FINANCIAL_TABLES"])
    names = ", ".join(f"public.{table}" for table in tables)
    op.execute(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE")


def upgrade() -> None:
    _lock_tables()
    _replace_functions(upgrade=True)
    op.execute("""
        DO $$
        BEGIN
            PERFORM public.financial_check_transfer(user_id,id) FROM public.transfer_links;
            PERFORM public.financial_check_action(refund.user_id,refund.action_id)
            FROM (
                SELECT DISTINCT i.user_id,i.action_id FROM public.financial_action_transactions i
                WHERE i.after_state->>'kind'='refund' AND i.after_state->'refund_action_id'=to_jsonb(i.action_id)
            ) refund;
        END $$
    """)


def downgrade() -> None:
    _lock_tables()
    _replace_functions(upgrade=False)
