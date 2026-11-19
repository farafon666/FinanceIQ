"""Approved sequential financial invariants C-01--C-11.

Owner-root serialization is deliberately a separate revision/task (2.8).
"""

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None

EVIDENCE_CHECK = """
schema_version = 1
AND jsonb_typeof(details->'accepted_plan') IS NOT DISTINCT FROM 'object'
AND jsonb_typeof(details->'result') IS NOT DISTINCT FROM 'object'
AND jsonb_typeof(details->'accepted_plan'->'parameters') IS NOT DISTINCT FROM 'object'
AND jsonb_typeof(details->'accepted_plan'->'consents') IS NOT DISTINCT FROM 'array'
AND details->'accepted_plan' @> jsonb_build_object(
    'user_id',user_id,'action_type',action_type,
    'account_id',account_id,'source_attempt_id',source_attempt_id)
AND (action_type NOT IN ('import_confirm','import_cancel') OR
    (source_attempt_id IS NOT NULL AND account_id IS NULL))
AND (action_type NOT IN ('confirm_balance','revoke_balance','confirm_history',
    'revoke_history','archive_account','restore_account') OR
    (account_id IS NOT NULL AND source_attempt_id IS NULL))
AND (action_type NOT IN ('import_cancel','revoke_balance','revoke_history') OR
    (reason IS NOT NULL AND btrim(reason) <> ''))
"""

# SECURITY DEFINER is restricted to fixed, schema-qualified operations. Runtime
# cannot execute these helpers directly or alter their search path/definitions.
FUNCTIONS: dict[str, tuple[str, str]] = {
    "financial_valid_state": (
        "snapshot jsonb",
        """
RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE key text;
BEGIN
    IF jsonb_typeof(snapshot) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
    IF NOT snapshot ?& ARRAY['kind','category_id','category_origin','categorization_rule_id',
            'refund_of_transaction_id','refund_action_id','last_action_id','revision','cancelled_by_id']
        OR (SELECT count(*) FROM jsonb_object_keys(snapshot))<>9
        OR (snapshot->>'kind' IN ('income','expense','transfer','refund','unclassified')) IS NOT TRUE
        OR (snapshot->>'category_origin' IN ('none','manual','rule')) IS NOT TRUE
        OR (snapshot->>'revision' ~ '^[1-9][0-9]*$') IS NOT TRUE THEN RETURN false;
    END IF;
    FOREACH key IN ARRAY ARRAY['category_id','categorization_rule_id','refund_of_transaction_id',
        'refund_action_id','last_action_id','cancelled_by_id'] LOOP
        IF (key='last_action_id' OR snapshot->key<>'null'::jsonb)
            AND (snapshot->>key ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') IS NOT TRUE THEN
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
END $$
""",
    ),
    "financial_state": (
        "value jsonb",
        """
RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
    SELECT value - ARRAY['id','user_id','created_at','account_id',
        'source_movement_id','occurred_on','amount','description']
$$
""",
    ),
    "financial_same_set": (
        "left_value jsonb, right_value jsonb",
        """
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN jsonb_typeof(left_value)='array' AND jsonb_typeof(right_value)='array'
        THEN left_value @> right_value AND right_value @> left_value
            AND jsonb_array_length(left_value)=jsonb_array_length(right_value)
        ELSE false END
$$
""",
    ),
    "financial_action": (
        "owner uuid, identity uuid, allowed text[], fresh boolean DEFAULT true",
        """
RETURNS public.financial_actions LANGUAGE plpgsql VOLATILE AS $$
DECLARE result public.financial_actions;
BEGIN
    SELECT a.* INTO result FROM public.financial_actions a
    WHERE a.user_id=owner AND a.id=identity AND a.action_type=ANY(allowed)
        AND (NOT fresh OR a.xmin=pg_current_xact_id()::xid);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Financial action does not prove this change' USING ERRCODE='23514';
    END IF;
    RETURN result;
END $$
""",
    ),
    "financial_prepare_fact": (
        "",
        """
RETURNS trigger LANGUAGE plpgsql VOLATILE AS $$
DECLARE a public.financial_actions; source_attempt uuid; allowed text[];
BEGIN
    IF TG_OP='UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
    IF TG_OP='INSERT' THEN
        a:=public.financial_action(NEW.user_id,NEW.last_action_id,ARRAY['import_confirm']);
        SELECT attempt_id INTO source_attempt FROM public.import_movements
            WHERE user_id=NEW.user_id AND id=NEW.source_movement_id;
        IF a.source_attempt_id IS DISTINCT FROM source_attempt THEN
            RAISE EXCEPTION 'Fact requires its own import confirmation' USING ERRCODE='23514';
        END IF;
        NEW.revision:=1;
    ELSE
        IF (public.financial_state(to_jsonb(NEW))-ARRAY['revision','last_action_id'])
            IS NOT DISTINCT FROM
           (public.financial_state(to_jsonb(OLD))-ARRAY['revision','last_action_id'])
           AND NEW.kind<>'transfer' THEN
            RAISE EXCEPTION 'Audit version requires a financial change' USING ERRCODE='23514';
        END IF;
        IF NEW.cancelled_by_id IS NOT NULL THEN
            allowed:=ARRAY['import_cancel'];
        ELSIF (NEW.refund_of_transaction_id,NEW.kind) IS DISTINCT FROM
              (OLD.refund_of_transaction_id,OLD.kind)
              AND (NEW.kind='refund' OR OLD.kind='refund') THEN
            allowed:=ARRAY['link_refund','unlink_refund','confirm_unlinked_refund','import_confirm'];
        ELSIF NEW.kind='transfer' OR OLD.kind='transfer' THEN
            allowed:=ARRAY['confirm_transfer','replace_transfer','unlink_transfer','import_confirm'];
        ELSIF NEW.kind IS DISTINCT FROM OLD.kind THEN
            allowed:=ARRAY['set_kind','import_confirm'];
        ELSE allowed:=ARRAY['set_category','import_confirm'];
        END IF;
        a:=public.financial_action(NEW.user_id,NEW.last_action_id,allowed);
        IF a.action_type='import_confirm' AND NEW.kind<>'transfer' AND OLD.kind<>'transfer'
            AND (NEW.kind,NEW.refund_of_transaction_id) IS NOT DISTINCT FROM
                (OLD.kind,OLD.refund_of_transaction_id) THEN
            RAISE EXCEPTION 'Link alone cannot rewrite existing classification' USING ERRCODE='23514';
        END IF;
        IF a.action_type='import_confirm' AND NEW.kind NOT IN ('transfer','refund')
            AND OLD.kind NOT IN ('transfer','refund') THEN
            RAISE EXCEPTION 'Import changes to existing facts require separate financial consent' USING ERRCODE='23514';
        END IF;
        IF NEW.last_action_id=OLD.last_action_id THEN
            RAISE EXCEPTION 'A new change requires a new action' USING ERRCODE='23514';
        END IF;
        IF OLD.refund_of_transaction_id IS NOT NULL AND NEW.refund_of_transaction_id IS NULL
            AND NEW.cancelled_by_id IS NULL AND
            (NEW.kind<>'unclassified' OR a.action_type<>'unlink_refund') THEN
            RAISE EXCEPTION 'Unlinking a refund requires unclassified state' USING ERRCODE='23514';
        END IF;
        IF a.action_type='unlink_transfer' AND NEW.kind<>'unclassified' THEN
            RAISE EXCEPTION 'Unlinking a transfer requires unclassified state' USING ERRCODE='23514';
        END IF;
        IF NEW.cancelled_by_id IS NULL THEN
            IF (NEW.kind,NEW.refund_of_transaction_id) IS DISTINCT FROM
               (OLD.kind,OLD.refund_of_transaction_id) AND NEW.kind='refund' THEN
                IF NEW.refund_action_id IS DISTINCT FROM NEW.last_action_id THEN
                    RAISE EXCEPTION 'Refund change requires its own evidence' USING ERRCODE='23514';
                END IF;
            ELSIF (NEW.kind,NEW.refund_of_transaction_id) IS NOT DISTINCT FROM
                  (OLD.kind,OLD.refund_of_transaction_id)
                  AND NEW.refund_action_id IS DISTINCT FROM OLD.refund_action_id THEN
                RAISE EXCEPTION 'Category changes preserve refund evidence' USING ERRCODE='23514';
            END IF;
        END IF;
        NEW.revision:=OLD.revision+1;
    END IF;
    IF NEW.category_id IS NOT NULL AND (TG_OP='INSERT' OR NEW.category_id IS DISTINCT FROM OLD.category_id)
        AND EXISTS(SELECT 1 FROM public.categories WHERE user_id=NEW.user_id
            AND id=NEW.category_id AND archived_at IS NOT NULL) THEN
        RAISE EXCEPTION 'New assignment requires an active category' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$
""",
    ),
    "financial_fact_effects": (
        "",
        """
RETURNS trigger LANGUAGE plpgsql VOLATILE AS $$
DECLARE cause uuid;
BEGIN
    IF TG_OP='UPDATE' THEN
        IF OLD.cancelled_by_id IS NOT NULL OR NEW.cancelled_by_id IS NULL THEN RETURN NULL; END IF;
        SELECT action_id INTO cause FROM public.import_cancellations
            WHERE user_id=NEW.user_id AND id=NEW.cancelled_by_id;
    ELSE cause:=NEW.last_action_id;
    END IF;
    IF cause IS NULL OR cause IS DISTINCT FROM NEW.last_action_id THEN
        RAISE EXCEPTION 'Fact change requires its originating action' USING ERRCODE='23514';
    END IF;
    UPDATE public.accounts SET facts_version=facts_version+1
        WHERE user_id=NEW.user_id AND id=NEW.account_id;
    UPDATE public.history_confirmations SET revocation_action_id=cause
        WHERE user_id=NEW.user_id AND account_id=NEW.account_id
            AND revocation_action_id IS NULL AND NEW.occurred_on BETWEEN start_date AND end_date;
    RETURN NULL;
END $$
""",
    ),
    "financial_check_transaction": (
        "owner uuid, identity uuid",
        """
RETURNS void LANGUAGE plpgsql VOLATILE AS $$
DECLARE t public.transactions; m public.import_movements; p public.import_attempts;
    original public.transactions; a public.financial_actions; category_kind text; count_links bigint;
BEGIN
    SELECT * INTO t FROM public.transactions WHERE user_id=owner AND id=identity;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT * INTO m FROM public.import_movements WHERE user_id=owner AND id=t.source_movement_id;
    SELECT * INTO p FROM public.import_attempts WHERE user_id=owner AND id=m.attempt_id;
    IF p.status IS DISTINCT FROM 'accepted' OR m.decision IS DISTINCT FROM 'insert'
        OR m.result_transaction_id IS DISTINCT FROM t.id
        OR (m.account_id,m.occurred_on,m.amount,m.source_comment) IS DISTINCT FROM
           (t.account_id,t.occurred_on,t.amount,t.description) THEN
        RAISE EXCEPTION 'Fact and accepted insert provenance disagree' USING ERRCODE='23514';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM public.financial_action_transactions i
        WHERE i.user_id=owner AND i.action_id=p.accepted_action_id AND i.transaction_id=t.id
            AND i.before_state='{}') THEN
        RAISE EXCEPTION 'Fact creation has no immutable audit item' USING ERRCODE='23514';
    END IF;
    IF t.cancelled_by_id IS NOT NULL THEN
        IF NOT EXISTS(SELECT 1 FROM public.import_cancellations c
            WHERE c.user_id=owner AND c.id=t.cancelled_by_id AND c.attempt_id=p.id) THEN
            RAISE EXCEPTION 'Cancellation does not own this insert' USING ERRCODE='23514';
        END IF;
        RETURN;
    END IF;
    IF EXISTS(SELECT 1 FROM public.import_cancellations WHERE user_id=owner AND attempt_id=p.id) THEN
        RAISE EXCEPTION 'Cancelled import retains a live insert' USING ERRCODE='23514';
    END IF;
    SELECT count(*) INTO count_links FROM public.transfer_links l WHERE l.user_id=owner
        AND l.ended_by_action_id IS NULL AND l.cancelled_by_id IS NULL
        AND (l.outgoing_transaction_id=t.id OR l.incoming_transaction_id=t.id);
    IF (t.kind='transfer' AND count_links<>1) OR (t.kind<>'transfer' AND count_links<>0) THEN
        RAISE EXCEPTION 'Transfer classification and active links disagree' USING ERRCODE='23514';
    END IF;
    IF t.kind='refund' THEN
        a:=public.financial_action(owner,t.refund_action_id,
            ARRAY['link_refund','confirm_unlinked_refund','import_confirm'],false);
        IF NOT EXISTS(SELECT 1 FROM public.financial_action_transactions i
            WHERE i.user_id=owner AND i.action_id=a.id AND i.transaction_id=t.id
                AND i.after_state->>'kind'='refund'
                AND i.after_state->'refund_of_transaction_id' IS NOT DISTINCT FROM to_jsonb(t.refund_of_transaction_id)) THEN
            -- to_jsonb(SQL NULL) is SQL NULL, whereas snapshots contain JSON null.
            IF NOT (t.refund_of_transaction_id IS NULL AND EXISTS(
                SELECT 1 FROM public.financial_action_transactions i WHERE i.user_id=owner
                    AND i.action_id=a.id AND i.transaction_id=t.id
                    AND i.after_state @> '{"kind":"refund","refund_of_transaction_id": null}')) THEN
                RAISE EXCEPTION 'Refund evidence does not describe the relationship' USING ERRCODE='23514';
            END IF;
        END IF;
        IF t.refund_of_transaction_id IS NULL THEN
            IF NOT (a.details->'accepted_plan'->'consents' @> '["unlinked_refund","unverified_limit"]') THEN
                RAISE EXCEPTION 'Unlinked refund requires explicit acknowledgement' USING ERRCODE='23514';
            END IF;
        ELSE
            SELECT * INTO original FROM public.transactions
                WHERE user_id=owner AND id=t.refund_of_transaction_id;
            IF original.cancelled_by_id IS NOT NULL OR original.kind NOT IN ('income','expense')
                OR sign(original.amount)=sign(t.amount) OR t.occurred_on<original.occurred_on
                OR (SELECT sum(abs(amount)) FROM public.transactions
                    WHERE user_id=owner AND refund_of_transaction_id=original.id
                        AND cancelled_by_id IS NULL)>abs(original.amount) THEN
                RAISE EXCEPTION 'Refund original, date, sign or total is invalid' USING ERRCODE='23514';
            END IF;
        END IF;
    END IF;
    IF t.category_id IS NOT NULL THEN
        SELECT kind INTO category_kind FROM public.categories WHERE user_id=owner AND id=t.category_id;
        IF category_kind IS DISTINCT FROM (CASE WHEN t.kind='refund'
                THEN CASE WHEN t.amount>0 THEN 'expense' ELSE 'income' END ELSE t.kind END) THEN
            RAISE EXCEPTION 'Direct category has the wrong direction' USING ERRCODE='23514';
        END IF;
    END IF;
END $$
""",
    ),
    "financial_check_transfer": (
        "owner uuid, identity uuid",
        """
RETURNS void LANGUAGE plpgsql VOLATILE AS $$
DECLARE l public.transfer_links; outgoing public.transactions; incoming public.transactions;
    a public.financial_actions; ending public.financial_actions; side uuid;
BEGIN
    SELECT * INTO l FROM public.transfer_links WHERE user_id=owner AND id=identity;
    IF NOT FOUND THEN RETURN; END IF;
    a:=public.financial_action(owner,l.confirmation_action_id,
        ARRAY['confirm_transfer','replace_transfer','import_confirm'],false);
    IF NOT (a.details->'accepted_plan'->'consents' @> '["own_accounts"]')
        OR NOT (a.details->'accepted_plan'->'parameters'->'transfer_links' @>
            jsonb_build_array(jsonb_build_object('id',l.id,'outgoing_transaction_id',
                l.outgoing_transaction_id,'incoming_transaction_id',l.incoming_transaction_id))) THEN
        RAISE EXCEPTION 'Transfer requires proof of the selected own-account sides' USING ERRCODE='23514';
    END IF;
    FOREACH side IN ARRAY ARRAY[l.outgoing_transaction_id,l.incoming_transaction_id] LOOP
        IF side IS NOT NULL AND NOT EXISTS(SELECT 1 FROM public.financial_action_transactions i
            WHERE i.user_id=owner AND i.action_id=a.id AND i.transaction_id=side
                AND i.after_state->>'kind'='transfer') THEN
            RAISE EXCEPTION 'Transfer side has no matching audit item' USING ERRCODE='23514';
        END IF;
    END LOOP;
    IF l.ended_by_action_id IS NOT NULL THEN
        ending:=public.financial_action(owner,l.ended_by_action_id,
            ARRAY['replace_transfer','unlink_transfer','import_confirm'],false);
        FOREACH side IN ARRAY ARRAY[l.outgoing_transaction_id,l.incoming_transaction_id] LOOP
            IF side IS NOT NULL AND NOT EXISTS(SELECT 1 FROM public.financial_action_transactions i
                WHERE i.user_id=owner AND i.action_id=ending.id AND i.transaction_id=side
                    AND (ending.action_type<>'unlink_transfer' OR i.after_state->>'kind'='unclassified')) THEN
                RAISE EXCEPTION 'Ending a transfer requires evidence for both sides' USING ERRCODE='23514';
            END IF;
        END LOOP;
    ELSIF l.cancelled_by_id IS NOT NULL THEN
        IF EXISTS(SELECT 1 FROM public.transactions t WHERE t.user_id=owner
            AND t.id IN (l.outgoing_transaction_id,l.incoming_transaction_id)
            AND t.cancelled_by_id IS DISTINCT FROM l.cancelled_by_id) THEN
            RAISE EXCEPTION 'Only an internal transfer can be cancelled' USING ERRCODE='23514';
        END IF;
    ELSE
        SELECT * INTO outgoing FROM public.transactions WHERE user_id=owner AND id=l.outgoing_transaction_id;
        SELECT * INTO incoming FROM public.transactions WHERE user_id=owner AND id=l.incoming_transaction_id;
        IF (l.outgoing_transaction_id IS NOT NULL AND (outgoing.cancelled_by_id IS NOT NULL
                OR outgoing.kind<>'transfer' OR outgoing.amount>=0))
            OR (l.incoming_transaction_id IS NOT NULL AND (incoming.cancelled_by_id IS NOT NULL
                OR incoming.kind<>'transfer' OR incoming.amount<=0))
            OR (l.outgoing_transaction_id IS NOT NULL AND l.incoming_transaction_id IS NOT NULL
                AND (outgoing.account_id=incoming.account_id OR outgoing.amount<>-incoming.amount)) THEN
            RAISE EXCEPTION 'Active transfer sides are inconsistent' USING ERRCODE='23514';
        END IF;
    END IF;
    PERFORM public.financial_check_transaction(owner,l.outgoing_transaction_id);
    PERFORM public.financial_check_transaction(owner,l.incoming_transaction_id);
END $$
""",
    ),
    "financial_check_batch": (
        "owner uuid, identity uuid",
        """
RETURNS void LANGUAGE plpgsql VOLATILE AS $$
DECLARE accepted_count bigint;
BEGIN
    SELECT count(*) INTO accepted_count FROM public.import_attempts p WHERE p.user_id=owner
        AND p.batch_id=identity AND p.status='accepted' AND NOT EXISTS(
            SELECT 1 FROM public.import_cancellations c WHERE c.user_id=owner AND c.attempt_id=p.id);
    IF accepted_count>1 OR (accepted_count=1 AND EXISTS(
        SELECT 1 FROM public.import_attempts p WHERE p.user_id=owner AND p.batch_id=identity
            AND p.status IN ('processing','preview','rejected'))) THEN
        RAISE EXCEPTION 'Batch already has a live accepted result' USING ERRCODE='23514';
    END IF;
END $$
""",
    ),
    "financial_check_attempt": (
        "owner uuid, identity uuid, accepting boolean DEFAULT false",
        """
RETURNS void LANGUAGE plpgsql VOLATILE AS $$
DECLARE p public.import_attempts; a public.financial_actions; actual jsonb; linked record;
    classifications jsonb; result_ids jsonb; classification_ids jsonb;
BEGIN
    SELECT * INTO p FROM public.import_attempts WHERE user_id=owner AND id=identity;
    IF NOT FOUND THEN RETURN; END IF;
    PERFORM public.financial_check_batch(owner,p.batch_id);
    IF p.status<>'accepted' THEN RETURN; END IF;
    a:=public.financial_action(owner,p.accepted_action_id,ARRAY['import_confirm'],false);
    IF a.source_attempt_id IS DISTINCT FROM p.id OR a.plan_digest<>p.preview_digest
        OR a.plan_version<>p.preview_version OR p.preview_plan IS NOT NULL
        OR a.details->'accepted_plan'->'parameters'->'parse_complete' IS DISTINCT FROM 'true'::jsonb
        OR a.details->'accepted_plan'->'parameters'->'errors' IS DISTINCT FROM '[]'::jsonb
        OR a.details->'accepted_plan'->'parameters'->'pending' IS DISTINCT FROM '[]'::jsonb THEN
        RAISE EXCEPTION 'Accepted import requires a complete matching plan' USING ERRCODE='23514';
    END IF;
    IF p.source_row_count<>(SELECT count(*) FROM public.import_rows WHERE user_id=owner AND attempt_id=p.id)
        OR p.movement_count<>(SELECT count(*) FROM public.import_movements WHERE user_id=owner AND attempt_id=p.id)
        OR EXISTS(SELECT 1 FROM public.import_rows WHERE user_id=owner AND attempt_id=p.id
            AND (source_values IS NULL OR raw_payload IS NOT NULL OR validation_errors<>'[]'))
        OR EXISTS(SELECT 1 FROM public.import_account_mappings m JOIN public.accounts c
            ON c.user_id=m.user_id AND c.id=m.account_id WHERE m.user_id=owner AND m.attempt_id=p.id
                AND accepting AND c.archived_at IS NOT NULL)
        OR EXISTS(SELECT 1 FROM public.import_account_mappings WHERE user_id=owner AND attempt_id=p.id AND account_id IS NULL)
        OR EXISTS(SELECT 1 FROM public.import_movements WHERE user_id=owner AND attempt_id=p.id
            AND (account_id IS NULL OR occurred_on IS NULL OR amount IS NULL OR source_comment IS NULL
                OR fingerprint IS NULL OR decision='pending' OR result_transaction_id IS NULL
                OR (side='outgoing' AND amount>=0) OR (side='incoming' AND amount<=0)))
        OR EXISTS(SELECT 1 FROM public.import_rows r WHERE r.user_id=owner AND r.attempt_id=p.id AND
            CASE WHEN r.sheet_name='Переводы' THEN
                (SELECT count(*) FROM public.import_movements m WHERE m.user_id=owner AND m.row_id=r.id)<>2
                OR NOT EXISTS(SELECT 1 FROM public.import_movements m WHERE m.user_id=owner AND m.row_id=r.id AND side='outgoing')
                OR NOT EXISTS(SELECT 1 FROM public.import_movements m WHERE m.user_id=owner AND m.row_id=r.id AND side='incoming')
            ELSE (SELECT count(*) FROM public.import_movements m WHERE m.user_id=owner AND m.row_id=r.id)<>1
                OR NOT EXISTS(SELECT 1 FROM public.import_movements m WHERE m.user_id=owner AND m.row_id=r.id AND side='single') END) THEN
        RAISE EXCEPTION 'Accepted import has incomplete rows, mappings or movements' USING ERRCODE='23514';
    END IF;
    SELECT coalesce(jsonb_agg(jsonb_build_object('movement_id',id,'transaction_id',
        result_transaction_id,'decision',decision)),'[]') INTO actual FROM public.import_movements
        WHERE user_id=owner AND attempt_id=p.id;
    IF NOT public.financial_same_set(a.details->'result'->'movements',actual)
        OR NOT public.financial_same_set(a.details->'accepted_plan'->'parameters'->'movements',actual)
        OR a.details->'result'->'source_row_count' IS DISTINCT FROM to_jsonb(p.source_row_count)
        OR a.details->'result'->'movement_count' IS DISTINCT FROM to_jsonb(p.movement_count) THEN
        RAISE EXCEPTION 'Accepted import result does not match its movements' USING ERRCODE='23514';
    END IF;
    classifications:=a.details->'result'->'classifications';
    IF jsonb_typeof(classifications) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'Accepted result must retain original classifications' USING ERRCODE='23514';
    END IF;
    SELECT coalesce(jsonb_agg(result_transaction_id),'[]') INTO result_ids
        FROM public.import_movements WHERE user_id=owner AND attempt_id=p.id;
    SELECT coalesce(jsonb_agg(saved->'transaction_id'),'[]') INTO classification_ids
        FROM jsonb_array_elements(classifications) saved;
    IF NOT public.financial_same_set(result_ids,classification_ids) OR EXISTS(
        SELECT 1 FROM jsonb_array_elements(classifications) saved WHERE
            NOT public.financial_valid_state(saved->'state')
            OR NOT EXISTS(SELECT 1 FROM public.transactions t WHERE t.user_id=owner
                AND to_jsonb(t.id)=saved->'transaction_id' AND (
                    public.financial_state(to_jsonb(t))=saved->'state'
                    OR EXISTS(SELECT 1 FROM public.financial_action_transactions i WHERE i.user_id=owner
                        AND i.transaction_id=t.id AND (i.before_state=saved->'state' OR i.after_state=saved->'state'))))) THEN
        RAISE EXCEPTION 'Saved classifications do not describe the accepted facts' USING ERRCODE='23514';
    END IF;
    FOR linked IN SELECT m.*,t.source_movement_id,t.occurred_on AS fact_date,t.amount AS fact_amount,
            t.account_id AS fact_account,t.cancelled_by_id FROM public.import_movements m
        JOIN public.transactions t ON t.user_id=m.user_id AND t.id=m.result_transaction_id
        WHERE m.user_id=owner AND m.attempt_id=p.id LOOP
        IF (linked.account_id,linked.occurred_on,linked.amount) IS DISTINCT FROM
            (linked.fact_account,linked.fact_date,linked.fact_amount)
            OR (linked.decision='link' AND linked.source_movement_id=linked.id)
            OR (linked.decision='link' AND linked.cancelled_by_id IS NOT NULL AND EXISTS(
                SELECT 1 FROM public.import_cancellations cancelled
                JOIN public.financial_actions cancellation_action ON cancellation_action.user_id=cancelled.user_id
                    AND cancellation_action.id=cancelled.action_id
                WHERE cancelled.user_id=owner AND cancelled.id=linked.cancelled_by_id
                    AND cancellation_action.created_at<=a.created_at))
            OR (linked.cancelled_by_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM public.import_cancellations WHERE user_id=owner AND attempt_id=p.id)) THEN
            RAISE EXCEPTION 'Import result references a different or cancelled fact' USING ERRCODE='23514';
        END IF;
        IF linked.decision='insert' THEN
            PERFORM public.financial_check_transaction(owner,linked.result_transaction_id);
        END IF;
    END LOOP;
END $$
""",
    ),
    "financial_check_cancellation": (
        "owner uuid, identity uuid",
        """
RETURNS void LANGUAGE plpgsql VOLATILE AS $$
DECLARE c public.import_cancellations; p public.import_attempts; a public.financial_actions;
    excluded uuid[]; excluded_json jsonb; preserved jsonb; facts_json jsonb;
BEGIN
    SELECT * INTO c FROM public.import_cancellations WHERE user_id=owner AND id=identity;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT * INTO p FROM public.import_attempts WHERE user_id=owner AND id=c.attempt_id;
    a:=public.financial_action(owner,c.action_id,ARRAY['import_cancel'],false);
    IF p.status<>'accepted' OR a.source_attempt_id IS DISTINCT FROM p.id THEN
        RAISE EXCEPTION 'Cancellation must identify its accepted result' USING ERRCODE='23514';
    END IF;
    SELECT coalesce(array_agg(result_transaction_id),'{}'),coalesce(jsonb_agg(result_transaction_id),'[]')
        INTO excluded,excluded_json FROM public.import_movements
        WHERE user_id=owner AND attempt_id=p.id AND decision='insert';
    SELECT coalesce(jsonb_agg(result_transaction_id),'[]') INTO preserved FROM public.import_movements
        WHERE user_id=owner AND attempt_id=p.id AND decision='link';
    SELECT coalesce(jsonb_agg(jsonb_build_object('transaction_id',id,'account_id',account_id,
        'occurred_on',occurred_on)),'[]') INTO facts_json FROM public.transactions WHERE user_id=owner AND id=ANY(excluded);
    IF NOT public.financial_same_set(a.details->'result'->'excluded_ids',excluded_json)
        OR NOT public.financial_same_set(a.details->'accepted_plan'->'parameters'->'excluded_ids',excluded_json)
        OR NOT public.financial_same_set(a.details->'result'->'preserved_link_ids',preserved)
        OR NOT public.financial_same_set(a.details->'result'->'facts',facts_json)
        OR EXISTS(SELECT 1 FROM public.transactions WHERE user_id=owner AND
            ((id=ANY(excluded) AND cancelled_by_id IS DISTINCT FROM c.id)
                OR (cancelled_by_id=c.id AND NOT id=ANY(excluded)))) THEN
        RAISE EXCEPTION 'Cancellation must exclude exactly its own inserts' USING ERRCODE='23514';
    END IF;
    IF EXISTS(SELECT 1 FROM public.import_movements m JOIN public.import_attempts other
            ON other.user_id=m.user_id AND other.id=m.attempt_id
        WHERE m.user_id=owner AND m.result_transaction_id=ANY(excluded) AND m.attempt_id<>p.id
            AND other.status='accepted' AND NOT EXISTS(SELECT 1 FROM public.import_cancellations x
                WHERE x.user_id=owner AND x.attempt_id=other.id))
        OR EXISTS(SELECT 1 FROM public.transfer_links l WHERE l.user_id=owner AND l.ended_by_action_id IS NULL
            AND (l.cancelled_by_id IS NULL OR l.cancelled_by_id=c.id) AND
            ((l.outgoing_transaction_id=ANY(excluded) OR l.incoming_transaction_id=ANY(excluded))
                AND l.cancelled_by_id IS DISTINCT FROM c.id
             OR l.confirmation_action_id=p.accepted_action_id AND l.cancelled_by_id IS NULL
                AND (coalesce(NOT l.outgoing_transaction_id=ANY(excluded),false)
                    OR coalesce(NOT l.incoming_transaction_id=ANY(excluded),false))))
        OR EXISTS(SELECT 1 FROM public.transactions r WHERE r.user_id=owner AND r.kind='refund'
            AND (r.cancelled_by_id IS NULL OR r.cancelled_by_id=c.id) AND
            ((r.id=ANY(excluded) AND r.refund_of_transaction_id IS NOT NULL
                AND NOT r.refund_of_transaction_id=ANY(excluded))
             OR (r.refund_of_transaction_id=ANY(excluded) AND NOT r.id=ANY(excluded))
             OR (r.refund_action_id=p.accepted_action_id AND r.cancelled_by_id IS NULL))) THEN
        RAISE EXCEPTION 'External observations or financial relationships block cancellation' USING ERRCODE='23514';
    END IF;
    PERFORM public.financial_check_batch(owner,p.batch_id);
END $$
""",
    ),
    "financial_check_action": (
        "owner uuid, identity uuid",
        """
RETURNS void LANGUAGE plpgsql VOLATILE AS $$
DECLARE a public.financial_actions; actual jsonb; expected_facts jsonb; p record;
BEGIN
    SELECT * INTO a FROM public.financial_actions WHERE user_id=owner AND id=identity;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT coalesce(jsonb_agg(transaction_id),'[]') INTO actual FROM public.financial_action_transactions
        WHERE user_id=owner AND action_id=a.id;
    IF NOT public.financial_same_set(coalesce(a.details->'result'->'transaction_ids','[]'),actual)
        OR (a.action_type IN ('set_kind','set_category','confirm_transfer','replace_transfer','unlink_transfer',
            'link_refund','unlink_refund','confirm_unlinked_refund') AND actual='[]') THEN
        RAISE EXCEPTION 'Action result and audit items disagree' USING ERRCODE='23514';
    END IF;
    IF EXISTS(SELECT 1 FROM public.financial_action_transactions i JOIN public.transactions t
        ON t.user_id=i.user_id AND t.id=i.transaction_id WHERE i.user_id=owner AND i.action_id=a.id AND (
            NOT public.financial_valid_state(i.after_state)
            OR (i.before_state<>'{}' AND NOT public.financial_valid_state(i.before_state))
            OR i.after_state->'last_action_id' IS DISTINCT FROM to_jsonb(a.id)
            OR (i.before_state='{}' AND (a.action_type<>'import_confirm' OR i.after_state->'revision'<>'1'::jsonb))
            OR NOT (i.after_state=public.financial_state(to_jsonb(t)) OR EXISTS(
                SELECT 1 FROM public.financial_action_transactions later WHERE later.user_id=owner
                    AND later.transaction_id=i.transaction_id AND later.before_state=i.after_state
                    AND later.after_state->'revision'>i.after_state->'revision')))) THEN
        RAISE EXCEPTION 'Stored audit states do not describe the financial history' USING ERRCODE='23514';
    END IF;
    IF a.action_type='import_confirm' THEN
        IF NOT EXISTS(SELECT 1 FROM public.import_attempts WHERE user_id=owner AND id=a.source_attempt_id
            AND accepted_action_id=a.id AND status='accepted') THEN
            RAISE EXCEPTION 'Confirm action requires an accepted result' USING ERRCODE='23514';
        END IF;
        SELECT coalesce(jsonb_agg(jsonb_build_object('transaction_id',t.id,'account_id',t.account_id,
            'occurred_on',t.occurred_on)),'[]') INTO expected_facts FROM public.transactions t
            JOIN public.import_movements m ON m.user_id=t.user_id AND m.id=t.source_movement_id
            WHERE t.user_id=owner AND m.attempt_id=a.source_attempt_id;
        IF NOT public.financial_same_set(a.details->'result'->'facts',expected_facts) THEN
            RAISE EXCEPTION 'Confirm evidence must retain affected fact IDs and dates' USING ERRCODE='23514';
        END IF;
    ELSIF a.action_type='import_cancel' THEN
        SELECT id INTO p FROM public.import_cancellations WHERE user_id=owner AND action_id=a.id;
        IF NOT FOUND THEN RAISE EXCEPTION 'Cancel action requires a cancellation' USING ERRCODE='23514'; END IF;
        PERFORM public.financial_check_cancellation(owner,p.id);
    ELSIF a.action_type IN ('confirm_balance','revoke_balance') THEN
        IF NOT EXISTS(SELECT 1 FROM public.balance_confirmations b WHERE b.user_id=owner
            AND b.account_id=a.account_id AND CASE WHEN a.action_type='confirm_balance'
                THEN b.confirmation_action_id=a.id ELSE b.revocation_action_id=a.id END) THEN
            RAISE EXCEPTION 'Balance action requires its target confirmation' USING ERRCODE='23514';
        END IF;
    ELSIF a.action_type IN ('confirm_history','revoke_history') THEN
        IF NOT EXISTS(SELECT 1 FROM public.history_confirmations h WHERE h.user_id=owner
            AND h.account_id=a.account_id AND CASE WHEN a.action_type='confirm_history'
                THEN h.confirmation_action_id=a.id ELSE h.revocation_action_id=a.id END) THEN
            RAISE EXCEPTION 'History action requires its target confirmation' USING ERRCODE='23514';
        END IF;
    ELSIF a.action_type IN ('confirm_transfer','replace_transfer','unlink_transfer') THEN
        IF NOT EXISTS(SELECT 1 FROM public.transfer_links l WHERE l.user_id=owner
            AND (l.confirmation_action_id=a.id OR l.ended_by_action_id=a.id)) THEN
            RAISE EXCEPTION 'Transfer action requires the recorded relationship change' USING ERRCODE='23514';
        END IF;
    END IF;
END $$
""",
    ),
    "financial_check_confirmation": (
        "owner uuid, identity uuid, is_balance boolean",
        """
RETURNS void LANGUAGE plpgsql VOLATILE AS $$
DECLARE stored jsonb; a public.financial_actions; expected jsonb; cause uuid;
BEGIN
    IF is_balance THEN
        SELECT to_jsonb(b) INTO stored FROM public.balance_confirmations b WHERE b.user_id=owner AND b.id=identity;
    ELSE
        SELECT to_jsonb(h) INTO stored FROM public.history_confirmations h WHERE h.user_id=owner AND h.id=identity;
    END IF;
    IF stored IS NULL THEN RETURN; END IF;
    a:=public.financial_action(owner,(stored->>'confirmation_action_id')::uuid,
        ARRAY[CASE WHEN is_balance THEN 'confirm_balance' ELSE 'confirm_history' END],false);
    expected:=jsonb_build_object('confirmation_id',identity);
    IF is_balance THEN
        expected:=expected || jsonb_build_object('balance_date',stored->'balance_date','balance',stored->>'balance');
        IF NOT (a.details->'accepted_plan'->'consents' @> '["eod_balance"]') THEN
            RAISE EXCEPTION 'Stored balance lacks EOD confirmation' USING ERRCODE='23514';
        END IF;
    ELSE
        expected:=expected || jsonb_build_object('start_date',stored->'start_date','end_date',stored->'end_date',
            'observed_facts_version',stored->'observed_facts_version',
            'observed_facts_digest',substr(stored->>'observed_facts_digest',3));
        IF NOT (a.details->'accepted_plan'->'consents' @> '["history_complete"]') THEN
            RAISE EXCEPTION 'Stored history lacks statement-review confirmation' USING ERRCODE='23514';
        END IF;
    END IF;
    IF a.account_id IS DISTINCT FROM (stored->>'account_id')::uuid
        OR NOT (a.details->'accepted_plan'->'parameters' @> expected)
        OR a.details->'result'->'confirmation_id' IS DISTINCT FROM to_jsonb(identity) THEN
        RAISE EXCEPTION 'Stored confirmation and its evidence disagree' USING ERRCODE='23514';
    END IF;
    cause:=(stored->>'revocation_action_id')::uuid;
    IF cause IS NOT NULL THEN
        a:=public.financial_action(owner,cause,CASE WHEN is_balance THEN ARRAY['revoke_balance']
            ELSE ARRAY['revoke_history','import_confirm','import_cancel'] END,false);
        IF a.action_type IN ('import_confirm','import_cancel') THEN
            IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(a.details->'result'->'facts') f
                WHERE f->'account_id'=stored->'account_id' AND (f->>'occurred_on')::date
                    BETWEEN (stored->>'start_date')::date AND (stored->>'end_date')::date) THEN
                RAISE EXCEPTION 'Stored automatic revocation lacks a changed fact' USING ERRCODE='23514';
            END IF;
        ELSIF a.account_id IS DISTINCT FROM (stored->>'account_id')::uuid
            OR NOT (a.details->'accepted_plan'->'parameters' @> jsonb_build_object('confirmation_id',identity))
            OR a.details->'result'->'confirmation_id' IS DISTINCT FROM to_jsonb(identity) THEN
            RAISE EXCEPTION 'Stored revocation targets a different confirmation' USING ERRCODE='23514';
        END IF;
    END IF;
END $$
""",
    ),
    "financial_fact_event": (
        "",
        """
RETURNS trigger LANGUAGE plpgsql VOLATILE AS $$
DECLARE before_value jsonb; after_value jsonb; related record;
BEGIN
    IF TG_OP='UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NULL; END IF;
    before_value:=CASE WHEN TG_OP='INSERT' THEN '{}'::jsonb ELSE public.financial_state(to_jsonb(OLD)) END;
    after_value:=public.financial_state(to_jsonb(NEW));
    IF NOT EXISTS(SELECT 1 FROM public.financial_action_transactions i WHERE i.user_id=NEW.user_id
        AND i.action_id=NEW.last_action_id AND i.transaction_id=NEW.id
        AND i.before_state=before_value AND i.after_state=after_value) THEN
        RAISE EXCEPTION 'Change requires exact before and after audit states' USING ERRCODE='23514';
    END IF;
    PERFORM public.financial_check_transaction(NEW.user_id,NEW.id);
    IF TG_OP='UPDATE' THEN
        IF OLD.kind IN ('income','expense') AND NEW.kind NOT IN ('income','expense')
            AND NEW.cancelled_by_id IS NULL AND EXISTS(SELECT 1 FROM public.transactions r
                WHERE r.user_id=NEW.user_id AND r.refund_of_transaction_id=NEW.id AND r.cancelled_by_id IS NULL) THEN
            RAISE EXCEPTION 'Original change invalidates existing refunds' USING ERRCODE='23514';
        END IF;
        PERFORM public.financial_check_transaction(OLD.user_id,OLD.refund_of_transaction_id);
    END IF;
    PERFORM public.financial_check_transaction(NEW.user_id,NEW.refund_of_transaction_id);
    FOR related IN SELECT id FROM public.transfer_links WHERE user_id=NEW.user_id
        AND (outgoing_transaction_id=NEW.id OR incoming_transaction_id=NEW.id) LOOP
        PERFORM public.financial_check_transfer(NEW.user_id,related.id);
    END LOOP;
    -- External live observations are checked once by the cancellation event.
    RETURN NULL;
END $$
""",
    ),
    "financial_accept_guard": (
        "",
        """
RETURNS trigger LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    -- Deferred final-state checks permit historical observations only after a
    -- later cancellation. They must not allow a new observation to be born
    -- cancelled and hidden by cancelling its own result in the same xact.
    IF NEW.status='accepted' AND OLD.status<>'accepted' AND EXISTS(
        SELECT 1 FROM public.import_movements m JOIN public.transactions t
            ON t.user_id=m.user_id AND t.id=m.result_transaction_id
        WHERE m.user_id=NEW.user_id AND m.attempt_id=NEW.id
            AND m.decision='link' AND t.cancelled_by_id IS NOT NULL) THEN
        RAISE EXCEPTION 'New link requires a currently active fact' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$
""",
    ),
    "financial_import_event": (
        "",
        """
RETURNS trigger LANGUAGE plpgsql VOLATILE AS $$
DECLARE a public.financial_actions;
BEGIN
    IF TG_TABLE_NAME='import_attempts' THEN
        IF TG_OP='INSERT' AND EXISTS(SELECT 1 FROM public.import_attempts other
            WHERE other.user_id=NEW.user_id AND other.batch_id=NEW.batch_id AND other.id<>NEW.id
                AND other.status='accepted' AND NOT EXISTS(SELECT 1 FROM public.import_cancellations c
                    WHERE c.user_id=NEW.user_id AND c.attempt_id=other.id)) THEN
            RAISE EXCEPTION 'A new attempt requires cancellation of the accepted result' USING ERRCODE='23514';
        END IF;
        IF TG_OP='UPDATE' AND NEW.status='accepted' AND OLD.status<>'accepted' THEN
            a:=public.financial_action(NEW.user_id,NEW.accepted_action_id,ARRAY['import_confirm']);
            IF OLD.preview_plan IS DISTINCT FROM a.details->'accepted_plan'
                OR OLD.preview_digest IS DISTINCT FROM NEW.preview_digest
                OR OLD.preview_version<>NEW.preview_version THEN
                RAISE EXCEPTION 'Accepted plan must be the reviewed preview' USING ERRCODE='23514';
            END IF;
            PERFORM public.financial_check_attempt(NEW.user_id,NEW.id,true);
        ELSE PERFORM public.financial_check_batch(NEW.user_id,NEW.batch_id);
        END IF;
    ELSIF TG_TABLE_NAME='import_cancellations' THEN
        PERFORM public.financial_action(NEW.user_id,NEW.action_id,ARRAY['import_cancel']);
        PERFORM public.financial_check_cancellation(NEW.user_id,NEW.id);
    ELSE PERFORM public.financial_check_attempt(NEW.user_id,NEW.attempt_id);
    END IF;
    RETURN NULL;
END $$
""",
    ),
    "financial_transfer_event": (
        "",
        """
RETURNS trigger LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        PERFORM public.financial_action(NEW.user_id,NEW.confirmation_action_id,
            ARRAY['confirm_transfer','replace_transfer','import_confirm']);
    ELSIF NEW.ended_by_action_id IS DISTINCT FROM OLD.ended_by_action_id THEN
        PERFORM public.financial_action(NEW.user_id,NEW.ended_by_action_id,
            ARRAY['replace_transfer','unlink_transfer','import_confirm']);
    END IF;
    PERFORM public.financial_check_transfer(NEW.user_id,NEW.id);
    RETURN NULL;
END $$
""",
    ),
    "financial_action_event": (
        "",
        """
RETURNS trigger LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    IF TG_TABLE_NAME='financial_action_transactions' THEN
        PERFORM public.financial_action(NEW.user_id,NEW.action_id,ARRAY[
            'import_confirm','import_cancel','set_kind','set_category','confirm_transfer',
            'replace_transfer','unlink_transfer','link_refund','unlink_refund','confirm_unlinked_refund']);
        IF NEW.after_state->'last_action_id' IS DISTINCT FROM to_jsonb(NEW.action_id)
            OR NOT EXISTS(SELECT 1 FROM public.transactions t WHERE t.user_id=NEW.user_id AND t.id=NEW.transaction_id
                AND (public.financial_state(to_jsonb(t))=NEW.after_state
                    OR EXISTS(SELECT 1 FROM public.financial_action_transactions later
                        WHERE later.user_id=NEW.user_id AND later.transaction_id=NEW.transaction_id
                            AND later.before_state=NEW.after_state
                            AND (later.after_state->>'revision')::bigint>(NEW.after_state->>'revision')::bigint))) THEN
            RAISE EXCEPTION 'Audit item does not correspond to an actual change' USING ERRCODE='23514';
        END IF;
        -- The action INSERT event validates the complete item set once. An item
        -- cannot be appended later because the action must belong to this xact.
    ELSE PERFORM public.financial_check_action(NEW.user_id,NEW.id);
    END IF;
    RETURN NULL;
END $$
""",
    ),
    "financial_guard_action": (
        "",
        """
RETURNS trigger LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    RAISE EXCEPTION 'Financial actions are immutable' USING ERRCODE='23514';
END $$
""",
    ),
    "financial_category_archive": (
        "",
        """
RETURNS trigger LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    IF OLD.archived_at IS NULL AND NEW.archived_at IS NOT NULL THEN
        UPDATE public.categorization_rules SET is_active=false WHERE user_id=NEW.user_id
            AND category_id=NEW.id AND is_active;
    END IF;
    RETURN NULL;
END $$
""",
    ),
    "financial_rule_event": (
        "",
        """
RETURNS trigger LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    IF EXISTS(SELECT 1 FROM public.categorization_rules r JOIN public.categories c
        ON c.user_id=r.user_id AND c.id=r.category_id WHERE r.user_id=NEW.user_id
            AND r.id=NEW.id AND r.is_active AND c.archived_at IS NOT NULL) THEN
        RAISE EXCEPTION 'Active rule requires an active category' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END $$
""",
    ),
    "financial_confirmation_event": (
        "",
        """
RETURNS trigger LANGUAGE plpgsql VOLATILE AS $$
DECLARE a public.financial_actions; expected jsonb; is_balance boolean; allowed text[]; cause uuid;
BEGIN
    is_balance:=TG_TABLE_NAME='balance_confirmations';
    IF TG_OP='UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NULL; END IF;
    IF TG_OP='INSERT' THEN
        cause:=NEW.confirmation_action_id;
        allowed:=ARRAY[CASE WHEN is_balance THEN 'confirm_balance' ELSE 'confirm_history' END];
    ELSE
        cause:=NEW.revocation_action_id;
        allowed:=CASE WHEN is_balance THEN ARRAY['revoke_balance'] ELSE
            ARRAY['revoke_history','import_confirm','import_cancel'] END;
    END IF;
    a:=public.financial_action(NEW.user_id,cause,allowed);
    IF a.action_type IN ('import_confirm','import_cancel') THEN
        IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(a.details->'result'->'facts') f
            WHERE f->>'account_id'=NEW.account_id::text
                AND (f->>'occurred_on')::date BETWEEN NEW.start_date AND NEW.end_date) THEN
            RAISE EXCEPTION 'Automatic revocation requires a changed fact in the interval' USING ERRCODE='23514';
        END IF;
    ELSE
        expected:=jsonb_build_object('confirmation_id',NEW.id);
        IF TG_OP='INSERT' THEN
            IF is_balance THEN
                expected:=expected || jsonb_build_object('balance_date',NEW.balance_date,'balance',NEW.balance::text);
                IF NOT (a.details->'accepted_plan'->'consents' @> '["eod_balance"]') THEN
                    RAISE EXCEPTION 'Balance confirmation requires EOD evidence' USING ERRCODE='23514';
                END IF;
            ELSE
                expected:=expected || jsonb_build_object('start_date',NEW.start_date,'end_date',NEW.end_date,
                    'observed_facts_version',NEW.observed_facts_version,
                    'observed_facts_digest',encode(NEW.observed_facts_digest,'hex'));
                IF NOT (a.details->'accepted_plan'->'consents' @> '["history_complete"]') THEN
                    RAISE EXCEPTION 'History confirmation requires statement-review evidence' USING ERRCODE='23514';
                END IF;
            END IF;
        END IF;
        IF a.account_id IS DISTINCT FROM NEW.account_id
            OR NOT (a.details->'accepted_plan'->'parameters' @> expected)
            OR a.details->'result'->'confirmation_id' IS DISTINCT FROM to_jsonb(NEW.id) THEN
            RAISE EXCEPTION 'Confirmation action targets a different statement' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NULL;
END $$
""",
    ),
    "financial_account_event": (
        "",
        """
RETURNS trigger LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    IF NEW.archived_at IS NOT DISTINCT FROM OLD.archived_at THEN RETURN NULL; END IF;
    IF NOT EXISTS(SELECT 1 FROM public.financial_actions a WHERE a.user_id=NEW.user_id
        AND a.account_id=NEW.id AND a.xmin=pg_current_xact_id()::xid
        AND a.action_type=CASE WHEN NEW.archived_at IS NULL THEN 'restore_account' ELSE 'archive_account' END
        AND a.details->'accepted_plan'->'parameters' @> jsonb_build_object(
            'before_archived_at',OLD.archived_at,'after_archived_at',NEW.archived_at)
        AND a.details->'result' @> jsonb_build_object('account_id',NEW.id,'archived_at',NEW.archived_at)) THEN
        RAISE EXCEPTION 'Account archive transition requires its own before and after evidence' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END $$
""",
    ),
}

TRIGGERS = (
    (
        "import_attempts",
        "financial_accept_guard",
        "BEFORE UPDATE",
        "financial_accept_guard",
        False,
    ),
    (
        "transactions",
        "financial_prepare_fact",
        "BEFORE INSERT OR UPDATE",
        "financial_prepare_fact",
        False,
    ),
    (
        "transactions",
        "financial_fact_effects",
        "AFTER INSERT OR UPDATE",
        "financial_fact_effects",
        False,
    ),
    (
        "transactions",
        "financial_fact_event",
        "AFTER INSERT OR UPDATE",
        "financial_fact_event",
        True,
    ),
    (
        "transfer_links",
        "financial_transfer_event",
        "AFTER INSERT OR UPDATE",
        "financial_transfer_event",
        True,
    ),
    (
        "financial_actions",
        "financial_guard_action",
        "BEFORE UPDATE OR DELETE",
        "financial_guard_action",
        False,
    ),
    (
        "financial_actions",
        "financial_action_event",
        "AFTER INSERT",
        "financial_action_event",
        True,
    ),
    (
        "financial_action_transactions",
        "financial_action_event",
        "AFTER INSERT",
        "financial_action_event",
        True,
    ),
    (
        "categories",
        "financial_category_archive",
        "AFTER UPDATE",
        "financial_category_archive",
        False,
    ),
    (
        "categorization_rules",
        "financial_rule_event",
        "AFTER INSERT OR UPDATE",
        "financial_rule_event",
        True,
    ),
    (
        "accounts",
        "financial_account_event",
        "AFTER UPDATE",
        "financial_account_event",
        True,
    ),
    *(
        (
            table,
            "financial_confirmation_event",
            "AFTER INSERT OR UPDATE",
            "financial_confirmation_event",
            True,
        )
        for table in ("balance_confirmations", "history_confirmations")
    ),
    # Child guards from 0005 already prevent accepted provenance edits. Complete
    # parse validation belongs only to acceptance, not to each staged row event.
    *(
        (
            table,
            "financial_import_event",
            "AFTER INSERT OR UPDATE",
            "financial_import_event",
            True,
        )
        for table in ("import_attempts", "import_cancellations")
    ),
)


def upgrade() -> None:
    # Triggers are not retroactive. Lock first, validate all pre-existing history,
    # and abort without backfills when the old permissive schema lacks evidence.
    op.execute(
        "LOCK TABLE accounts, balance_confirmations, history_confirmations, categories, categorization_rules, financial_actions, import_batches, import_attempts, import_account_mappings, import_rows, import_movements, import_cancellations, transactions, transfer_links, financial_action_transactions IN ACCESS EXCLUSIVE MODE"
    )
    op.create_check_constraint("evidence_v1", "financial_actions", EVIDENCE_CHECK)
    for name, (arguments, body) in FUNCTIONS.items():
        # The SET clause applies even to SQL helpers; public objects are qualified.
        body = body.replace(
            " AS $$", " SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$", 1
        )
        op.execute(f"CREATE FUNCTION public.{name}({arguments}) {body}")
        signature = arguments.replace(" DEFAULT true", "").replace(" DEFAULT false", "")
        op.execute(
            f"REVOKE ALL ON FUNCTION public.{name}({signature}) FROM PUBLIC, financeiq_runtime"
        )
    op.execute("""
        DO $$ DECLARE r record; BEGIN
            FOR r IN SELECT user_id,id FROM public.transactions LOOP
                PERFORM public.financial_check_transaction(r.user_id,r.id);
            END LOOP;
            FOR r IN SELECT user_id,id FROM public.transfer_links LOOP
                PERFORM public.financial_check_transfer(r.user_id,r.id);
            END LOOP;
            FOR r IN SELECT user_id,id FROM public.import_attempts LOOP
                PERFORM public.financial_check_attempt(r.user_id,r.id);
            END LOOP;
            FOR r IN SELECT user_id,id FROM public.import_cancellations LOOP
                PERFORM public.financial_check_cancellation(r.user_id,r.id);
            END LOOP;
            FOR r IN SELECT user_id,id FROM public.financial_actions LOOP
                PERFORM public.financial_check_action(r.user_id,r.id);
            END LOOP;
            FOR r IN SELECT user_id,id FROM public.balance_confirmations LOOP
                PERFORM public.financial_check_confirmation(r.user_id,r.id,true);
            END LOOP;
            FOR r IN SELECT user_id,id FROM public.history_confirmations LOOP
                PERFORM public.financial_check_confirmation(r.user_id,r.id,false);
            END LOOP;
            IF EXISTS(SELECT 1 FROM public.categorization_rules rule JOIN public.categories c
                ON c.user_id=rule.user_id AND c.id=rule.category_id WHERE rule.is_active AND c.archived_at IS NOT NULL)
                OR EXISTS(SELECT 1 FROM public.accounts a WHERE a.facts_version<>(
                    SELECT count(*)+count(t.cancelled_by_id) FROM public.transactions t
                    WHERE t.user_id=a.user_id AND t.account_id=a.id))
                OR EXISTS(SELECT 1 FROM public.accounts account WHERE account.archived_at IS NOT NULL
                    AND NOT EXISTS(SELECT 1 FROM public.financial_actions action WHERE action.user_id=account.user_id
                        AND action.account_id=account.id AND action.action_type='archive_account'
                        AND action.details->'result' @> jsonb_build_object('account_id',account.id,'archived_at',account.archived_at))) THEN
                RAISE EXCEPTION 'Legacy financial history is incompatible with approved invariants' USING ERRCODE='23514';
            END IF;
        END $$
    """)
    for table, name, event, function, deferred in TRIGGERS:
        constraint = "CONSTRAINT " if deferred else ""
        timing = "DEFERRABLE INITIALLY DEFERRED " if deferred else ""
        op.execute(
            f"CREATE {constraint}TRIGGER {name} {event} ON public.{table} {timing}FOR EACH ROW EXECUTE FUNCTION public.{function}()"
        )
    op.execute("REVOKE UPDATE (revision) ON public.transactions FROM financeiq_runtime")


def downgrade() -> None:
    op.execute(
        "LOCK TABLE accounts, balance_confirmations, history_confirmations, categories, categorization_rules, financial_actions, import_attempts, import_account_mappings, import_rows, import_movements, import_cancellations, transactions, transfer_links, financial_action_transactions IN ACCESS EXCLUSIVE MODE"
    )
    for table, name, _, _, _ in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER {name} ON public.{table}")
    for name, (arguments, _) in reversed(FUNCTIONS.items()):
        signature = arguments.replace(" DEFAULT true", "").replace(" DEFAULT false", "")
        op.execute(f"DROP FUNCTION public.{name}({signature})")
    op.drop_constraint(
        op.f("ck_financial_actions_evidence_v1"), "financial_actions", type_="check"
    )
    op.execute("GRANT UPDATE (revision) ON public.transactions TO financeiq_runtime")
