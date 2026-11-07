"""Real exact row validators, fictional sources, no transaction or posting service."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import localcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.business import credit_rows as legacy
from coinpup_api.business.credit_plan import CreditSource, plan_credit_line
from coinpup_api.business.credit_revision_plan import CreditMemberIdentity
from coinpup_api.business.credit_revision_rows import (
    CreditRevisionOriginal,
    prepare_credit_revision_rows,
)
from coinpup_api.business.credit_revision_schemas import CreditReplacement
from coinpup_api.business.models import BusinessDocument
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import Journal, JournalLine
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_storage import DIMENSION_FIELDS, prepare_journal_lines


def fixture(kind="invoice", *, count=3, selected=2, asset=ASSETS["USD"], net="17.23", tax="2.77"):
    ledger, credit, account = uuid4(), uuid4(), uuid4()
    document = BusinessDocument(
        id=uuid4(), ledger_id=ledger, document_kind=kind, asset_id=asset.asset_id, party_id=uuid4()
    )
    catalog, sources, current, source_operations = {asset.asset_id: asset}, [], [], {}
    for index in range(count):
        n, t = Amount.parse(net, asset), Amount.parse(tax, asset)
        source = CreditSource(
            ledger,
            document.id,
            uuid4(),
            kind,
            uuid4(),
            uuid4(),
            n,
            t,
            n + t,
            Amount(asset, 0),
            Amount(asset, 0),
        )
        sources.append(source)
        source_operations[source.line_id] = uuid4()
        if index >= selected:
            continue
        group = legacy.credit_rows(
            ledger,
            document,
            SimpleNamespace(reason=f"Fictional old {index}"),
            plan_credit_line(ledger, source, net, tax),
            account,
            catalog,
        )
        version = 1 if index == 0 else 3
        journal = Journal(
            id=group.journal_id,
            ledger_id=ledger,
            operation_id=group.operation_id,
            operation_version=version,
            journal_kind="posting",
            reverses_journal_id=None,
            transaction_date=date(2026, 10, 20),
            recognition_date=date(2026, 10, 11 + (index % 10)),
            description=group.description,
            revision_reason=None if version == 1 else "Old correction",
            sealed=True,
            created_at=datetime(2026, 10, 20, tzinfo=UTC),
        )
        identity = CreditMemberIdentity(
            ledger, credit, index + 1, source.line_id, group.operation_id, version, group.journal_id
        )
        current.append(
            CreditRevisionOriginal(
                identity,
                source_operations[source.line_id],
                journal,
                tuple(JournalLine(**row) for row in group.parameters),
            )
        )
    return SimpleNamespace(
        ledger=ledger,
        credit=credit,
        document=document,
        current=tuple(current),
        sources=sources,
        source_operations=source_operations,
        catalog=catalog,
        account=account,
    )


def assemble(s, positions, **overrides):
    selected = [] if positions is None else [s.sources[position] for position in positions]
    replacement = (
        None
        if positions is None
        else CreditReplacement(
            credit_date="2026-11-06",
            reason="Fictional replacement 中文",
            lines=[
                dict(
                    source_line_id=source.line_id,
                    net_amount=source.net.to_string(),
                    tax_amount=source.tax.to_string(),
                    recognition_date="2026-11-05",
                )
                for source in selected
            ],
        )
    )
    plans = {
        source.line_id: plan_credit_line(
            s.ledger, source, source.net.to_string(), source.tax.to_string()
        )
        for source in selected
    }
    args = dict(
        replacement=replacement,
        plans=plans,
        source_operations={
            source.line_id: s.source_operations[source.line_id] for source in selected
        },
        account_id=s.account if selected else None,
        catalog=s.catalog,
        reason="Fictional group correction",
    )
    return prepare_credit_revision_rows(
        s.ledger, s.credit, s.document, s.current, **(args | overrides)
    )


def assert_reversed(prepared, original, catalog):
    header = prepared.header
    assert header["id"] != original.journal.id
    assert header["reverses_journal_id"] == original.journal.id
    assert header["operation_id"] == original.identity.operation_id
    assert header["operation_version"] == original.identity.operation_version + 1
    assert header["journal_kind"] == "reversal"
    for field in ("transaction_date", "recognition_date", "description"):
        assert header[field] == getattr(original.journal, field)
    assert not ({"sealed", "created_at"} & header.keys())
    assert len(prepared.lines) == len(original.lines)
    for previous, row, parameters in zip(
        original.lines, prepared.lines, prepared.parameters, strict=True
    ):
        assert row.id != previous.id
        assert row.journal_id == header["id"]
        assert row.amount == -Amount.from_decimal(previous.amount, catalog[previous.asset_id])
        assert parameters["amount"] == previous.amount.copy_negate()
        for field in (
            "ledger_id",
            "line_no",
            "component_no",
            "role",
            "asset_id",
            "account_id",
            "category_id",
            *DIMENSION_FIELDS,
        ):
            assert getattr(row, field) == getattr(previous, field) == parameters[field]


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_complete_replacement_order_preserves_operations_and_all_original_facts(kind):
    s = fixture(kind)
    originals = {item.identity.source_line_id: item for item in s.current}
    before = [
        (
            item.journal.description,
            item.journal.operation_version,
            tuple(line.amount for line in item.lines),
        )
        for item in s.current
    ]
    with localcontext() as context:
        context.prec = 1
        result = assemble(s, [2, 0])
        assert [item.change.action for item in result] == ["create", "correct", "cancel"]
        assert [item.operation_version for item in result] == [1, 2, 4]
        for item in result:
            assert item.source_operation_id == s.source_operations[item.change.source_line_id]
            if item.reversal:
                assert_reversed(item.reversal, originals[item.change.source_line_id], s.catalog)
                assert item.reversal.header["revision_reason"] == "Fictional group correction"
            if item.replacement:
                new = item.replacement
                assert new.parameters == tuple(prepare_journal_lines(new.lines, s.catalog))
                assert new.header["transaction_date"] == date(2026, 11, 6)
                assert new.header["recognition_date"] == date(2026, 11, 5)
                assert new.header["description"] == "Fictional replacement 中文"
                assert new.header["revision_reason"] == (
                    "Fictional group correction" if item.change.action == "correct" else None
                )
                assert not ({"sealed", "created_at"} & new.header.keys())
                assert new.lines[0].amount + new.lines[1].amount == Amount(s.catalog["USD"], 0)
                assert all(row.document_line_id == item.change.source_line_id for row in new.lines)
                assert new.header["id"] not in {old.journal.id for old in s.current}
        assert result[1].operation_id == s.current[0].identity.operation_id
        assert result[2].operation_id == s.current[1].identity.operation_id
        assert result[0].operation_id not in {old.identity.operation_id for old in s.current}
        assert result[2].replacement is None and result[2].plan is None
    assert before == [
        (
            item.journal.description,
            item.journal.operation_version,
            tuple(line.amount for line in item.lines),
        )
        for item in s.current
    ]


def test_removed_source_rejoins_with_new_identity_while_retained_operation_advances():
    s = fixture()
    first = assemble(s, [0, 2])
    s.current = tuple(
        CreditRevisionOriginal(
            CreditMemberIdentity(
                s.ledger,
                s.credit,
                item.change.replacement_line_no,
                item.change.source_line_id,
                item.operation_id,
                item.operation_version,
                item.replacement.header["id"],
            ),
            item.source_operation_id,
            Journal(**item.replacement.header),
            tuple(JournalLine(**row) for row in item.replacement.parameters),
        )
        for item in first
        if item.replacement
    )
    second = assemble(s, [1, 2])
    assert second[0].change.action == "create" and second[0].operation_version == 1
    assert second[0].operation_id != first[-1].operation_id
    assert second[1].operation_id == first[1].operation_id and second[1].operation_version == 2
    assert second[0].replacement.header["revision_reason"] is None


def test_cancellation_can_decode_disabled_asset_without_changing_dates_or_any_dimensions():
    s = fixture(selected=1)
    old = s.current[0]
    # Exercise full generic reversal copying, not a new supported credit fee business type.
    old.lines[0].counterparty_entity_id, old.lines[0].dimension_owner_id = uuid4(), uuid4()
    fee = [
        JournalLine(
            **(
                dict(
                    id=uuid4(),
                    journal_id=old.journal.id,
                    ledger_id=s.ledger,
                    line_no=i + 3,
                    component_no=1,
                    role="account" if i == 0 else "expense",
                    asset_id="USD",
                    account_id=s.account if i == 0 else None,
                    category_id=uuid4() if i else None,
                    amount=Amount(s.catalog["USD"], -1 if i == 0 else 1).to_decimal(),
                )
            )
        )
        for i in range(2)
    ]
    s.current = (replace(old, lines=(*old.lines, *fee)),)
    disabled = {"USD": replace(s.catalog["USD"], enabled=False)}
    result = assemble(s, None, catalog=disabled)
    assert result[0].replacement is None and result[0].plan is None
    assert_reversed(result[0].reversal, s.current[0], disabled)
    with pytest.raises(MoneyError) as failure:
        assemble(s, [0], catalog=disabled)
    assert failure.value.code == "asset_disabled"


@pytest.mark.parametrize("asset", [ASSETS["USD"], ASSETS["BTC"], ASSETS["ETH"]])
def test_maximum_and_smallest_unit_are_exact_under_low_decimal_precision(asset):
    maximum, minimum = Amount(asset, 10 ** (20 + asset.scale) - 1), Amount(asset, 1)
    s = fixture(
        count=1,
        selected=1,
        asset=asset,
        net=(maximum - minimum).to_string(),
        tax=minimum.to_string(),
    )
    with localcontext() as context:
        context.prec = 1
        result = assemble(s, [0])[0]
        assert_reversed(result.reversal, s.current[0], s.catalog)
        assert abs(result.replacement.lines[0].amount.minor_units) == maximum.minor_units
        assert result.replacement.parameters == tuple(
            prepare_journal_lines(result.replacement.lines, s.catalog)
        )


@pytest.mark.parametrize(
    "selected,positions", [(200, tuple(range(200))), (100, tuple(range(100, 200)))]
)
def test_actual_200_source_bound_keeps_every_retained_or_replaced_member(selected, positions):
    s = fixture(count=200, selected=selected, net="0.01", tax="0")
    result = assemble(s, positions)
    assert len(result) == 200
    assert sum(item.replacement is not None for item in result) == len(positions)
    assert sum(item.reversal is not None for item in result) == selected
    assert len({item.operation_id for item in result}) == 200
    assert len(
        {
            journal.header["id"]
            for item in result
            for journal in (item.reversal, item.replacement)
            if journal
        }
    ) == (400 if selected == 200 else 200)


@pytest.mark.parametrize(
    "fault",
    [
        "journal",
        "version",
        "kind",
        "line_scope",
        "unbalanced",
        "catalog",
        "overflow",
        "source_operation",
    ],
)
def test_bad_current_identity_or_incomplete_original_cannot_produce_validated_rows(fault):
    s = fixture(selected=1)
    old = s.current[0]
    if fault == "journal":
        old.journal.id = uuid4()
    elif fault == "version":
        old.journal.operation_version += 1
    elif fault == "kind":
        old.journal.journal_kind = "reversal"
    elif fault == "line_scope":
        old.lines[0].ledger_id = uuid4()
    elif fault == "unbalanced":
        old.lines[0].amount = Amount(s.catalog["USD"], -1).to_decimal()
    elif fault == "catalog":
        s.catalog = {}
    elif fault == "overflow":
        s.current = (replace(old, identity=replace(old.identity, operation_version=2147483647)),)
    else:
        s.current = (replace(old, source_operation_id=old.identity.operation_id),)
    with pytest.raises(MoneyError):
        assemble(s, None)


@pytest.mark.parametrize(
    "fault", ["plans", "source_operations", "reason", "account", "amount", "document"]
)
def test_replacement_maps_and_plans_must_match_the_complete_request(fault):
    s = fixture(selected=1)
    source = s.sources[0]
    changed = {}
    if fault in {"plans", "source_operations"}:
        changed[fault] = {}
    elif fault == "reason":
        changed["reason"] = " "
    elif fault == "account":
        changed["account_id"] = None
    else:
        plan = plan_credit_line(s.ledger, source, source.net.to_string(), source.tax.to_string())
        changed["plans"] = {
            source.line_id: replace(
                plan,
                **(
                    {"net": Amount(s.catalog["USD"], 1)}
                    if fault == "amount"
                    else {"document_id": uuid4()}
                ),
            )
        }
    with pytest.raises(MoneyError):
        assemble(s, [0], **changed)


def test_legacy_credit_rows_keeps_uuid_call_order_dimensions_and_real_prebind_results(monkeypatch):
    s = fixture(selected=1)
    source = s.sources[0]
    plan = plan_credit_line(s.ledger, source, source.net.to_string(), source.tax.to_string())
    operation, journal = uuid4(), uuid4()
    values, consumed = iter((operation, journal)), []

    def generate():
        value = next(values)
        consumed.append(value)
        return value

    monkeypatch.setattr(legacy, "uuid4", generate)
    group = legacy.credit_rows(
        s.ledger,
        s.document,
        SimpleNamespace(reason="Fictional original"),
        plan,
        s.account,
        s.catalog,
    )
    assert consumed == [operation, journal]
    assert (group.operation_id, group.journal_id, group.description) == (
        operation,
        journal,
        "Fictional original",
    )
    assert group.parameters == tuple(prepare_journal_lines(group.lines, s.catalog))
    assert [row.line_no for row in group.lines] == [1, 2]
    assert (
        group.lines[0].account_id == s.account and group.lines[1].category_id == source.category_id
    )
    assert all(
        row.document_id == s.document.id
        and row.document_line_id == source.line_id
        and row.project_id == source.project_id
        and row.party_id == s.document.party_id
        for row in group.lines
    )
