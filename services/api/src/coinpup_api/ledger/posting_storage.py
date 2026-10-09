"""Row-aware pre-bind boundary for application posting writes.

PostgreSQL NUMERIC applies its typmod before a CHECK/trigger can see extra fractional
digits. This boundary accepts Amount objects, validates the actual row asset against
the catalog, and only then creates exact Decimal parameters. Direct administrative SQL
does not pass through this application boundary.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from uuid import UUID, uuid4

from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount

DIMENSION_FIELDS = (
    "project_id",
    "party_id",
    "counterparty_entity_id",
    "document_id",
    "document_line_id",
    "dimension_owner_id",
)


@dataclass(frozen=True, slots=True)
class PostingLine:
    journal_id: UUID
    ledger_id: UUID
    line_no: int
    role: str
    asset_id: str
    amount: Amount
    account_id: UUID | None = None
    category_id: UUID | None = None
    id: UUID = field(default_factory=uuid4)
    component_no: int = 0
    project_id: UUID | None = None
    party_id: UUID | None = None
    counterparty_entity_id: UUID | None = None
    document_id: UUID | None = None
    document_line_id: UUID | None = None
    dimension_owner_id: UUID | None = None


def prepare_journal_lines(
    lines: Sequence[PostingLine], catalog: Mapping[str, AssetDefinition]
) -> list[dict]:
    """Validate every row before producing any bind parameters; never round a quantity."""
    if len(lines) < 2:
        raise MoneyError("journal_incomplete", "A journal must have at least two lines.")
    result = []
    totals = {}
    positions = set()
    components = {}
    journal = lines[0].journal_id
    ledger = lines[0].ledger_id
    for line in lines:
        if not isinstance(line.amount, Amount):
            raise MoneyError("amount_type", "Journal quantities must be validated Amount objects.")
        definition = catalog.get(line.asset_id)
        if (
            definition is None
            or definition.asset_id != line.asset_id
            or line.amount.asset != definition
        ):
            raise MoneyError("asset_mismatch", "Journal quantity does not match its row asset.")
        # Revalidate minimum units and range even if a caller tampered with a frozen object.
        quantity = Amount(definition, line.amount.minor_units)
        if quantity.minor_units == 0:
            raise MoneyError("amount_zero", "Journal lines cannot have zero quantity.")
        if line.journal_id != journal or line.ledger_id != ledger:
            raise MoneyError(
                "journal_scope", "Journal lines must have the same journal and ledger."
            )
        if type(line.line_no) is not int or line.line_no < 1 or line.line_no in positions:
            raise MoneyError(
                "journal_position", "Journal line positions must be positive and unique."
            )
        positions.add(line.line_no)
        if type(line.component_no) is not int or not 0 <= line.component_no <= 20:
            raise MoneyError("journal_component", "Journal component number is invalid.")
        components.setdefault(line.component_no, []).append(line)
        if line.role == "account":
            valid = line.account_id is not None and line.category_id is None
        elif line.role in {"income", "expense"}:
            valid = line.account_id is None and line.category_id is not None
        elif line.role in {"equity", "exchange"}:
            valid = line.account_id is None and line.category_id is None
        else:
            valid = False
        if not valid:
            raise MoneyError("journal_role", "Journal references do not match the line role.")
        dimensions = {name: getattr(line, name) for name in DIMENSION_FIELDS}
        if (
            any(value is not None and not isinstance(value, UUID) for value in dimensions.values())
            or (line.document_line_id is not None and line.document_id is None)
            or ((line.counterparty_entity_id is None) != (line.dimension_owner_id is None))
        ):
            raise MoneyError("journal_dimension", "Journal dimension references are invalid.")
        total_key = (line.component_no, line.asset_id)
        totals[total_key] = totals.get(total_key, 0) + quantity.minor_units
        result.append(
            {
                "id": line.id,
                "journal_id": line.journal_id,
                "ledger_id": line.ledger_id,
                "line_no": line.line_no,
                "component_no": line.component_no,
                "role": line.role,
                "asset_id": line.asset_id,
                "amount": quantity.to_decimal(),
                "account_id": line.account_id,
                "category_id": line.category_id,
                **dimensions,
            }
        )
    if any(totals.values()):
        raise MoneyError(
            "journal_unbalanced", "Journal lines must balance separately for every asset."
        )
    if sorted(components) != list(range(len(components))):
        raise MoneyError(
            "journal_component", "Journal components must start at zero and be contiguous."
        )
    for number, component in components.items():
        if number == 0:
            continue
        accounts = [line for line in component if line.role == "account"]
        expenses = [line for line in component if line.role == "expense"]
        if (
            len(component) != 2
            or len(accounts) != 1
            or len(expenses) != 1
            or accounts[0].amount.minor_units >= 0
            or expenses[0].amount.minor_units <= 0
        ):
            raise MoneyError(
                "journal_component", "A fee component must debit an account and record an expense."
            )
    return result


def prepare_reversal_lines(source_lines, reversal_journal_id, catalog):
    """Reverse complete original posting rows exactly, including fee signs and components.

    This is not a bypass for arbitrary negative fee input: each output row is generated
    from a validated original row with identical references and an exact integer negation.
    """
    originals = [
        PostingLine(
            id=line.id,
            journal_id=line.journal_id,
            ledger_id=line.ledger_id,
            line_no=line.line_no,
            component_no=line.component_no,
            role=line.role,
            asset_id=line.asset_id,
            amount=Amount.from_decimal(line.amount, catalog[line.asset_id]),
            account_id=line.account_id,
            category_id=line.category_id,
            **{name: getattr(line, name) for name in DIMENSION_FIELDS},
        )
        for line in source_lines
    ]
    validated = prepare_journal_lines(originals, catalog)
    reversed_lines = [
        replace(line, id=uuid4(), journal_id=reversal_journal_id, amount=-line.amount)
        for line in originals
    ]
    parameters = [
        {
            **source,
            "id": reversal.id,
            "journal_id": reversal.journal_id,
            "amount": reversal.amount.to_decimal(),
        }
        for source, reversal in zip(validated, reversed_lines, strict=True)
    ]
    return reversed_lines, parameters
