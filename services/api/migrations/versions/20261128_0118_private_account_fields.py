"""Encrypt private account fields with the independently held v1 key."""

import unicodedata

import sqlalchemy as sa
from alembic import op
from coinpup_private_v1 import load_key

revision = "20261128_0118"
down_revision = "20261127_0117"
branch_labels = None
depends_on = None

_ACCOUNT = {
    "card_number": 19,
    "crypto_platform_name": 80,
    "crypto_platform_uid": 160,
    "crypto_platform_email": 254,
    "crypto_platform_note": 1000,
}
_ASSET = {"crypto_address": 128, "crypto_memo": 256, "crypto_label": 160}
_TABLES = (("accounts", _ACCOUNT), ("account_assets", _ASSET))
_ENVELOPE = "^v1[.][A-Za-z0-9_-]{1,32}[.][A-Za-z0-9+/]{38,}={0,2}$"
_SUFFIX = (
    "jsonb_typeof(details->'card_last_four') = 'string' "
    "AND details->>'card_last_four' ~ '^[0-9]{4}$'"
)


def _lock():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE public.accounts, public.account_assets IN ACCESS EXCLUSIVE MODE")


def _drop_field_checks():
    op.drop_constraint("ck_accounts_card_number", "accounts")
    op.drop_constraint("ck_accounts_crypto_platform", "accounts")
    op.drop_constraint("ck_account_assets_crypto_network", "account_assets")
    op.execute("DROP TRIGGER tr_account_assets_crypto_profile_history ON public.account_assets")


def _field_checks(encrypted):
    suffix = "_ciphertext" if encrypted else ""
    card = f"card_number{suffix}"
    name, uid, email, note = [
        f"crypto_platform_{key}{suffix}" for key in ("name", "uid", "email", "note")
    ]
    address, memo, label = [f"{key}{suffix}" for key in _ASSET]
    card_details = (
        "details ? 'card_last_four'"
        if encrypted
        else "card_number ~ '^[0-9]{12,19}$' AND "
        "details->>'card_last_four' IS NOT DISTINCT FROM right(card_number, 4)"
    )
    op.create_check_constraint(
        "ck_accounts_card_number",
        "accounts",
        f"{card} IS NULL OR (account_class = 'money' AND {card_details})",
    )
    op.create_check_constraint(
        "ck_accounts_crypto_platform",
        "accounts",
        f"""
        (crypto_platform_key IS NULL AND crypto_platform_id IS NULL AND {name} IS NULL
         AND {uid} IS NULL AND {email} IS NULL AND {note} IS NULL)
        OR (crypto_platform_key IS NOT NULL AND crypto_profile_used AND account_class = 'money'
            AND (kind IS DISTINCT FROM 'crypto' OR custody_mode IS NOT NULL)
            AND ((crypto_platform_id IS NOT NULL AND {name} IS NULL
                  AND crypto_platform_key = 'catalog:' || crypto_platform_id)
                 OR (crypto_platform_id IS NULL AND {name} IS NOT NULL AND btrim({name}) <> ''
                     AND crypto_platform_key ~ '^custom:[0-9a-f]{{64}}$')))
        """,
    )
    op.create_check_constraint(
        "ck_account_assets_crypto_network",
        "account_assets",
        "(crypto_network IS NOT NULL AND crypto_network ~ '^[a-z][a-z0-9_-]{0,63}$') OR "
        f"(crypto_network IS NULL AND {address} IS NULL AND {memo} IS NULL)",
    )
    op.execute(f"""
        CREATE TRIGGER tr_account_assets_crypto_profile_history
            BEFORE INSERT OR UPDATE OF crypto_network, {address}, {memo}, {label}
            ON public.account_assets FOR EACH ROW
            WHEN (num_nonnulls(NEW.crypto_network, NEW.{address}, NEW.{memo}, NEW.{label}) > 0)
            EXECUTE FUNCTION public.coinpup_guard_crypto_asset_profile_history()
    """)
    op.execute(f"""
        CREATE OR REPLACE FUNCTION public.coinpup_track_card_account_history() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.card_schema_used THEN
                    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Card account history exists',
                        CONSTRAINT='ck_accounts_card_history';
                END IF;
                RETURN OLD;
            END IF;
            NEW.card_schema_used := NEW.kind IS NOT DISTINCT FROM 'debit_card'
                OR NEW.{card} IS NOT NULL OR
                (CASE WHEN TG_OP = 'UPDATE' THEN OLD.card_schema_used ELSE false END);
            RETURN NEW;
        END;
        $$;
    """)


def _backfill(db, key, table, fields):
    # Transform only registered account data. The stream uses a bounded server-side cursor.
    # Keep arbitrary historical JSON as Postgres text, including its exact numeric values.
    accounts = table == "accounts"
    columns = ", ".join(fields)
    selected = [columns, "ledger_id", "id AS account_id" if accounts else "account_id, asset_id"]
    assignments = [f"{field}_ciphertext = :{field}_ciphertext" for field in fields]
    where = " OR ".join(f"{field} IS NOT NULL" for field in fields)
    if accounts:
        selected += [
            "crypto_platform_key",
            "CASE WHEN card_number IS NOT NULL THEN jsonb_build_object('card_last_four', "
            "right(card_number, 4)) "
            f"WHEN {_SUFFIX} THEN jsonb_build_object('card_last_four', "
            "details->>'card_last_four') ELSE '{}'::jsonb END::text AS public_details",
            f"NULLIF(CASE WHEN {_SUFFIX} THEN details - 'card_last_four' ELSE details END, "
            "'{}'::jsonb)::text AS private_details",
        ]
        assignments += [
            "crypto_platform_key = :platform_key",
            "details = CAST(:public_details AS jsonb)",
            "details_ciphertext = :details_ciphertext",
            "private_details_used = :used",
        ]
        where += " OR details <> '{}'::jsonb"
    identity = (
        "id = :account_id" if accounts else "account_id = :account_id AND asset_id = :asset_id"
    )
    select = sa.text(f"SELECT {', '.join(selected)} FROM public.{table} WHERE {where}")
    update = sa.text(f"UPDATE public.{table} SET {', '.join(assignments)} WHERE {identity}")

    with db.execute(select.execution_options(yield_per=128)) as stream:
        for rows in stream.mappings().partitions(128):
            updates = []
            for row in rows:
                ledger, account = row["ledger_id"], row["account_id"]
                asset = row.get("asset_id")
                values = {"account_id": account}
                if not accounts:
                    values["asset_id"] = asset
                for field in fields:
                    raw = row[field]
                    values[f"{field}_ciphertext"] = (
                        None
                        if raw is None
                        else key.encrypt(raw, table, field, ledger, account, asset)
                    )
                if accounts:
                    name = row["crypto_platform_name"]
                    index = row["crypto_platform_key"]
                    if name is not None:
                        normalized = " ".join(
                            unicodedata.normalize("NFKC", name).casefold().split()
                        )
                        index = key.custom_platform_key(normalized, ledger)
                    private = row["private_details"]
                    values.update(
                        public_details=row["public_details"],
                        used=private is not None,
                        platform_key=index,
                        details_ciphertext=(
                            None
                            if private is None
                            else key.encrypt(private, "accounts", "details", ledger, account)
                        ),
                    )
                updates.append(values)
            db.execute(update, updates)


def upgrade():
    if op.get_context().as_sql:
        # Offline SQL has neither a row stream nor an independently mounted key. Never stamp an
        # offline script's target as encrypted or embed the key in generated SQL.
        op.execute("DO $$ BEGIN RAISE EXCEPTION '0118 requires online Alembic upgrade'; END $$")
        return
    key = load_key()
    _lock()
    db = op.get_bind()
    op.create_table(
        "private_field_key_check",
        sa.Column("singleton", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("verifier", sa.Text(), nullable=False),
        sa.CheckConstraint("singleton = 1", name="ck_private_field_key_check_singleton"),
    )
    db.execute(
        sa.text("INSERT INTO private_field_key_check(singleton, verifier) VALUES (1, :verifier)"),
        {
            "verifier": key.encrypt(
                "coinpup-key-check", "private_field_key_check", "verifier", "", "1"
            )
        },
    )
    _drop_field_checks()
    op.add_column("accounts", sa.Column("details_ciphertext", sa.Text()))
    op.add_column(
        "accounts",
        sa.Column("private_details_used", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    for table, fields in _TABLES:
        for field in fields:
            op.add_column(table, sa.Column(field + "_ciphertext", sa.Text()))
        # A storage-only rewrite must not advance mobile cursors or change business versions.
        # Only the notification trigger is suspended, while the transaction holds both table locks.
        trigger = f"trg_{table}_change_log"
        op.execute(f"ALTER TABLE public.{table} DISABLE TRIGGER {trigger}")
        _backfill(db, key, table, fields)
        op.execute("SET CONSTRAINTS ALL IMMEDIATE")
        op.execute(f"ALTER TABLE public.{table} ENABLE TRIGGER {trigger}")
        for field in fields:
            op.drop_column(table, field)
        protected = list(fields) + (["details"] if table == "accounts" else [])
        op.create_check_constraint(
            f"ck_{table}_private_envelopes",
            table,
            " AND ".join(
                f"({f}_ciphertext IS NULL OR {f}_ciphertext ~ '{_ENVELOPE}')" for f in protected
            ),
        )
    _field_checks(encrypted=True)
    op.create_check_constraint(
        "ck_accounts_details_public",
        "accounts",
        "details = '{}'::jsonb OR (jsonb_typeof(details->'card_last_four') = 'string' "
        "AND details = jsonb_build_object('card_last_four', details->>'card_last_four') "
        "AND COALESCE(details->>'card_last_four' ~ '^[0-9]{4}$', false))",
    )
    op.execute("""
        CREATE FUNCTION public.coinpup_track_private_details() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.private_details_used THEN
                    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Private account history exists',
                        CONSTRAINT='ck_accounts_private_details_history';
                END IF;
                RETURN OLD;
            END IF;
            NEW.private_details_used := NEW.details_ciphertext IS NOT NULL OR
                (CASE WHEN TG_OP = 'UPDATE' THEN OLD.private_details_used ELSE false END);
            RETURN NEW;
        END; $$;
        CREATE TRIGGER tr_accounts_private_details_history
            BEFORE INSERT OR UPDATE OF details_ciphertext, private_details_used OR DELETE
            ON public.accounts FOR EACH ROW EXECUTE FUNCTION public.coinpup_track_private_details()
    """)
    # Force ALTER TABLE's snapshot-based heap rewrite: it copies only visible rows and nulls
    # dropped attributes. CLUSTER can copy recently-dead versions from this same transaction.
    # Use an independent scratch column so existing trigger dependencies are untouched.
    for table, _ in _TABLES:
        op.execute(f"ALTER TABLE public.{table} ADD COLUMN rewrite_marker boolean")
        op.execute(
            f"ALTER TABLE public.{table} ALTER COLUMN rewrite_marker TYPE integer "
            "USING (CASE WHEN rewrite_marker THEN 1 ELSE 0 END)"
        )
        op.execute(f"ALTER TABLE public.{table} DROP COLUMN rewrite_marker")
        op.execute(f"ANALYZE public.{table}")


def downgrade():
    _lock()
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.accounts
                       WHERE card_schema_used OR crypto_profile_used OR private_details_used)
            THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Private account history exists',
                    CONSTRAINT='ck_private_fields_downgrade';
            END IF;
        END; $$;
        DROP TRIGGER tr_accounts_private_details_history ON public.accounts;
        DROP FUNCTION public.coinpup_track_private_details();
    """)
    _drop_field_checks()
    op.drop_constraint("ck_accounts_details_public", "accounts")
    for table, fields in _TABLES:
        op.drop_constraint(f"ck_{table}_private_envelopes", table)
        for name, size in fields.items():
            op.drop_column(table, name + "_ciphertext")
            op.add_column(table, sa.Column(name, sa.String(size)))
    op.drop_column("accounts", "details_ciphertext")
    op.drop_column("accounts", "private_details_used")
    _field_checks(encrypted=False)
    op.drop_table("private_field_key_check")
