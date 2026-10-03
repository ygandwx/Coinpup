"""Compatibility facade for atomic postings, revisions and exact balances."""

from coinpup_api.ledger import readers as _readers
from coinpup_api.ledger.balances import BalanceQueries
from coinpup_api.ledger.commands.classified import ClassifiedCommands
from coinpup_api.ledger.commands.exchange import ExchangeCommands
from coinpup_api.ledger.commands.fees import FeeCommands
from coinpup_api.ledger.commands.transfer import TransferCommands
from coinpup_api.ledger.common import PostingCore
from coinpup_api.ledger.common import _invalid_money as _invalid_money
from coinpup_api.ledger.common import asset_definition as asset_definition
from coinpup_api.ledger.idempotency import _COMMON_LEGACY_FIELDS as _COMMON_LEGACY_FIELDS
from coinpup_api.ledger.idempotency import _IDEMPOTENCY_KEY as _IDEMPOTENCY_KEY
from coinpup_api.ledger.idempotency import _LEGACY_HASH_FIELDS as _LEGACY_HASH_FIELDS
from coinpup_api.ledger.idempotency import _RECEIPT as _RECEIPT
from coinpup_api.ledger.idempotency import CommandIdempotency
from coinpup_api.ledger.idempotency import command_hash as command_hash
from coinpup_api.ledger.readers import PostingReaders
from coinpup_api.ledger.revisions import RevisionService


class PostingService(
    ClassifiedCommands,
    TransferCommands,
    ExchangeCommands,
    FeeCommands,
    CommandIdempotency,
    PostingReaders,
    BalanceQueries,
    PostingCore,
):
    """Use the same owner transaction and ledger lock order as structure maintenance."""

    def correct_operation(self, owner_id, ledger_id, operation_id, payload, idempotency_key):
        return RevisionService(self.engine).correct(
            owner_id, ledger_id, operation_id, payload, idempotency_key
        )

    def cancel_operation(self, owner_id, ledger_id, operation_id, payload, idempotency_key):
        return RevisionService(self.engine).cancel(
            owner_id, ledger_id, operation_id, payload, idempotency_key
        )

    def history(self, owner_id, ledger_id, operation_id, limit=100, offset=0):
        return RevisionService(self.engine).read_history(
            owner_id, ledger_id, operation_id, limit, offset
        )


# Readers retain the original facade class dispatch without importing this module.
_readers.PostingService = PostingService
