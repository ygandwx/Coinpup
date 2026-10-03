"""Independent entities, ledgers, accounts, assets and category trees."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261003_0003"
down_revision = "20261003_0002"
branch_labels = None
depends_on = None


def _version_columns() -> list[sa.Column]:
    return [
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "assets",
        sa.Column("asset_id", sa.String(200), nullable=False),
        sa.Column("code", sa.String(8), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("scale", sa.Integer(), nullable=False),
        sa.Column("network", sa.String(64), nullable=True),
        sa.Column("token_reference", sa.String(128), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_version_columns(),
        sa.PrimaryKeyConstraint("asset_id"),
        sa.CheckConstraint("version > 0", name="ck_assets_version"),
        sa.CheckConstraint("scale BETWEEN 0 AND 18", name="ck_assets_scale"),
        sa.CheckConstraint(
            "(kind = 'fiat' AND code ~ '^[A-Z]{3}$' "
            "AND code NOT IN ('BTC', 'ETH', 'XMR') "
            "AND (code NOT IN ('USD', 'GBP', 'EUR', 'HKD', 'CNY') OR scale = 2) "
            "AND network IS NULL AND token_reference IS NULL AND asset_id = code) OR "
            "(kind = 'native' AND token_reference IS NULL AND asset_id = code "
            "AND network IS NOT NULL AND "
            "((code = 'BTC' AND network = 'bitcoin' AND scale = 8) OR "
            "(code = 'ETH' AND network = 'ethereum' AND scale = 18) OR "
            "(code = 'XMR' AND network = 'monero' AND scale = 12))) OR "
            "(kind = 'token' AND code IN ('USDT', 'USDC') "
            "AND network IS NOT NULL AND network ~ '^[a-z][a-z0-9_-]{0,63}$' "
            "AND token_reference IS NOT NULL "
            "AND token_reference ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$' "
            "AND asset_id = 'token:' || network || ':' || token_reference)",
            name="ck_assets_identity",
        ),
    )
    assets = sa.table(
        "assets",
        sa.column("asset_id", sa.String(200)),
        sa.column("code", sa.String(8)),
        sa.column("kind", sa.String(8)),
        sa.column("scale", sa.Integer()),
        sa.column("network", sa.String(64)),
    )
    op.bulk_insert(
        assets,
        [
            {"asset_id": code, "code": code, "kind": "fiat", "scale": 2, "network": None}
            for code in ("USD", "GBP", "EUR", "HKD", "CNY")
        ]
        + [
            {
                "asset_id": code,
                "code": code,
                "kind": "native",
                "scale": scale,
                "network": network,
            }
            for code, network, scale in (
                ("BTC", "bitcoin", 8),
                ("ETH", "ethereum", 18),
                ("XMR", "monero", 12),
            )
        ],
    )
    op.create_table(
        "entities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("legal_name", sa.String(200), nullable=True),
        sa.Column("country_code", sa.String(2), nullable=True),
        sa.Column("region_code", sa.String(16), nullable=True),
        sa.Column("company_type", sa.String(64), nullable=True),
        sa.Column("registration_date", sa.Date(), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *_version_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "owner_id", name="uq_entities_id_owner"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["administrators.id"], name="fk_entities_owner", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("kind IN ('personal', 'company')", name="ck_entities_kind"),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_entities_name"),
        sa.CheckConstraint("version > 0", name="ck_entities_version"),
        sa.CheckConstraint("jsonb_typeof(details) = 'object'", name="ck_entities_details"),
        sa.CheckConstraint(
            "country_code IS NULL OR country_code ~ '^[A-Z]{2}$'",
            name="ck_entities_country_code",
        ),
    )
    op.create_index("ix_entities_owner_id", "entities", ["owner_id"])
    op.create_table(
        "ledgers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("base_asset_id", sa.String(200), nullable=False),
        *_version_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["entity_id", "owner_id"],
            ["entities.id", "entities.owner_id"],
            name="fk_ledgers_entity_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["base_asset_id"],
            ["assets.asset_id"],
            name="fk_ledgers_base_asset",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("entity_id", name="uq_ledgers_entity"),
        sa.CheckConstraint("version > 0", name="ck_ledgers_version"),
    )
    op.create_index("ix_ledgers_owner_id", "ledgers", ["owner_id"])
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *_version_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["ledger_id"], ["ledgers.id"], name="fk_accounts_ledger", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("id", "ledger_id", name="uq_accounts_id_ledger"),
        sa.CheckConstraint(
            "kind IN ('bank', 'cash', 'wechat', 'alipay', 'credit_card', "
            "'paypal', 'wise', 'stripe', 'crypto')",
            name="ck_accounts_kind",
        ),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_accounts_name"),
        sa.CheckConstraint("version > 0", name="ck_accounts_version"),
        sa.CheckConstraint("jsonb_typeof(details) = 'object'", name="ck_accounts_details"),
    )
    op.create_index("ix_accounts_ledger_id", "accounts", ["ledger_id"])
    op.create_table(
        "account_assets",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.String(200), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("account_id", "asset_id"),
        sa.ForeignKeyConstraint(
            ["account_id", "ledger_id"],
            ["accounts.id", "accounts.ledger_id"],
            name="fk_account_assets_account_ledger",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["assets.asset_id"], name="fk_account_assets_asset", ondelete="RESTRICT"
        ),
    )
    op.create_index("ix_account_assets_ledger_id", "account_assets", ["ledger_id"])
    op.create_table(
        "categories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("name_en", sa.String(200), nullable=True),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("template_key", sa.String(64), nullable=True),
        sa.Column("archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *_version_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "ledger_id", "kind", name="uq_categories_id_ledger_kind"),
        sa.ForeignKeyConstraint(
            ["ledger_id"], ["ledgers.id"], name="fk_categories_ledger", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["parent_id", "ledger_id", "kind"],
            ["categories.id", "categories.ledger_id", "categories.kind"],
            name="fk_categories_parent_ledger_kind",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("kind IN ('income', 'expense')", name="ck_categories_kind"),
        sa.CheckConstraint("parent_id IS NULL OR parent_id <> id", name="ck_categories_parent"),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_categories_name"),
        sa.CheckConstraint("version > 0", name="ck_categories_version"),
    )
    op.create_index("ix_categories_ledger_id", "categories", ["ledger_id"])
    op.execute("""
        CREATE FUNCTION coinpup_structure_immutable_fields() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE field_name text;
        BEGIN
            FOREACH field_name IN ARRAY TG_ARGV LOOP
                IF (to_jsonb(NEW) -> field_name) IS DISTINCT FROM
                   (to_jsonb(OLD) -> field_name) THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'Structure identity is immutable',
                        CONSTRAINT = 'ck_structure_immutable';
                END IF;
            END LOOP;
            RETURN NEW;
        END;
        $$
    """)
    for table, fields in (
        ("assets", ("asset_id", "code", "kind", "scale", "network", "token_reference")),
        ("entities", ("id", "owner_id", "kind")),
        ("ledgers", ("id", "entity_id", "owner_id", "base_asset_id")),
        ("accounts", ("id", "ledger_id")),
        ("account_assets", ("account_id", "asset_id", "ledger_id")),
        ("categories", ("id", "ledger_id", "kind", "parent_id")),
    ):
        arguments = ", ".join(f"'{field}'" for field in fields)
        op.execute(
            f"CREATE TRIGGER tr_{table}_immutable BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION "
            f"coinpup_structure_immutable_fields({arguments})"
        )
    # A regular immediate FK alone permits cyclic references in one multi-row INSERT.
    # Requiring the parent before each insert, then freezing parent_id, keeps trees acyclic.
    op.execute("""
        CREATE FUNCTION coinpup_category_existing_parent() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NEW.parent_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.categories
                WHERE id = NEW.parent_id AND ledger_id = NEW.ledger_id AND kind = NEW.kind
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '23503',
                    MESSAGE = 'Category parent must already exist in this ledger and kind',
                    CONSTRAINT = 'fk_categories_parent_ledger_kind';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute(
        "CREATE TRIGGER tr_categories_existing_parent BEFORE INSERT ON categories "
        "FOR EACH ROW EXECUTE FUNCTION coinpup_category_existing_parent()"
    )


def downgrade() -> None:
    op.drop_table("categories")
    op.drop_table("account_assets")
    op.drop_table("accounts")
    op.drop_table("ledgers")
    op.drop_table("entities")
    op.drop_table("assets")
    op.execute("DROP FUNCTION coinpup_category_existing_parent()")
    op.execute("DROP FUNCTION coinpup_structure_immutable_fields()")
