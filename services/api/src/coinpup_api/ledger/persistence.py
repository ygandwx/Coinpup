"""A PostgreSQL bind boundary that accepts validated Amounts, never raw numeric inputs."""

from sqlalchemy import Numeric
from sqlalchemy.types import TypeDecorator

from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import MAX_FRACTION_DIGITS, MAX_INTEGER_DIGITS, Amount

NUMERIC_PRECISION = MAX_INTEGER_DIGITS + MAX_FRACTION_DIGITS
NUMERIC_SCALE = MAX_FRACTION_DIGITS


class AmountNumeric(TypeDecorator[Amount]):
    """Asset-specific NUMERIC(38,18), including exact Decimal decoding on SELECT.

    This is a typed write boundary, not a guarantee about arbitrary SQL. PostgreSQL can round
    over-scale raw SQL inputs before CHECK constraints see them. Multi-asset rows will require
    an explicit row-aware boundary; this fixed-asset column does not infer assets from a row.
    """

    impl = Numeric(NUMERIC_PRECISION, NUMERIC_SCALE, asdecimal=True)
    cache_ok = True

    def __init__(self, asset: AssetDefinition):
        if not isinstance(asset, AssetDefinition):
            raise MoneyError(
                "asset_required", "AmountNumeric requires a validated asset definition."
            )
        self.asset = asset
        super().__init__()

    @property
    def python_type(self):
        return Amount

    def load_dialect_impl(self, dialect):
        if dialect.name != "postgresql" or dialect.driver != "psycopg":
            raise MoneyError(
                "unsupported_database", "Exact amount storage requires PostgreSQL with psycopg."
            )
        return dialect.type_descriptor(Numeric(NUMERIC_PRECISION, NUMERIC_SCALE, asdecimal=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, Amount):
            raise MoneyError(
                "amount_required", "Bind a validated Amount, not a raw number or string."
            )
        if value.asset != self.asset:
            raise MoneyError(
                "asset_mismatch", "Bound amount does not match the column asset definition."
            )
        # Recheck units/range at the persistence boundary, independent of how an object arrived.
        return Amount(value.asset, value.minor_units).to_decimal()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return Amount.from_decimal(value, self.asset)
