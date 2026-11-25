import 'dart:convert';
import 'dart:io';

import 'package:coinpup_mobile/mobile_workspace.dart';
import 'package:coinpup_mobile/offline_store.dart';
import 'package:coinpup_mobile/ui/mobile_adapter.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:path/path.dart' as path;
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

void main() {
  sqfliteFfiInit();
  const scope = OfflineScope('fictional-local-source', 'owner-a');
  const original = FrozenIntent(
    id: 'intent-a',
    ledgerId: 'ledger-a',
    commandKey: 'stable-original-key',
    bodyJson: '{"amount":"10.010000000000000001","kind":"expense"}',
  );

  test(
    'reopen keeps original body, key and uncertain attempt in its owner scope',
    () async {
      final dir = await Directory.systemTemp.createTemp(
        'coinpup-offline-test-',
      );
      addTearDown(() => dir.delete(recursive: true));
      final file = path.join(dir.path, 'offline.sqlite');
      final first = await OfflineStore.open(databaseFactoryFfi, file, scope);
      await first.enqueue(original);
      await first.markAttempted(original.id);
      await first.close();

      final second = await OfflineStore.open(databaseFactoryFfi, file, scope);
      final resumed = (await second.nextIntent())!;
      expect(
        (resumed.id, resumed.commandKey, resumed.bodyJson, resumed.attempted),
        (original.id, original.commandKey, original.bodyJson, true),
      );
      await expectLater(
        second.enqueue(
          const FrozenIntent(
            id: 'intent-a',
            ledgerId: 'ledger-a',
            commandKey: 'stable-original-key',
            bodyJson: '{"amount":"99.00"}',
          ),
        ),
        throwsStateError,
      );
      await second.close();

      final other = await OfflineStore.open(
        databaseFactoryFfi,
        file,
        const OfflineScope('fictional-local-source', 'owner-b'),
      );
      expect(await other.nextIntent(), isNull);
      await other.close();

      final third = await OfflineStore.open(databaseFactoryFfi, file, scope);
      addTearDown(third.close);
      const receipt = '{"amount":"10.010000000000000001","version":1}';
      await third.settle(original.id, receipt);
      await third.settle(original.id, receipt);
      await expectLater(third.settle(original.id, '{}'), throwsStateError);
      expect(await third.nextIntent(), isNull);
    },
  );

  test(
    'ack cursor and tombstone are atomic and old records cannot revive',
    () async {
      final store = await OfflineStore.open(
        databaseFactoryFfi,
        inMemoryDatabasePath,
        scope,
      );
      addTearDown(store.close);
      const initial = CachedOperation(
        ledgerId: 'ledger-a',
        id: 'one',
        version: 1,
        json: '{"amount":"1.00"}',
      );
      await store.commitWindow(
        after: '0',
        delivered: '9007199254740995',
        witness: 'witness-a',
        changes: [initial],
      );
      expect((await store.operations('ledger-a')).single.json, initial.json);

      const different = CachedOperation(
        ledgerId: 'ledger-a',
        id: 'one',
        version: 1,
        json: '{}',
      );
      await expectLater(
        store.commitWindow(
          after: '9007199254740995',
          delivered: '9007199254740996',
          witness: 'not-confirmed',
          changes: [different],
        ),
        throwsStateError,
      );
      expect(await store.checkpoint(), (
        seq: '9007199254740995',
        witness: 'witness-a',
      ));

      const terminal = CachedOperation(
        ledgerId: 'ledger-a',
        id: 'one',
        version: 2,
        json: '{"state":"cancelled"}',
        removed: true,
      );
      await store.commitWindow(
        after: '9007199254740995',
        delivered: '9007199254740996',
        witness: 'witness-b',
        changes: [terminal],
      );
      await store.commitWindow(
        after: '9007199254740996',
        delivered: '9007199254740997',
        witness: 'witness-c',
        changes: [initial],
      );
      expect(await store.operations('ledger-a'), isEmpty);
      expect(await store.checkpoint(), (
        seq: '9007199254740997',
        witness: 'witness-c',
      ));
    },
  );

  test(
    'reopened posting cache retains exact read-only legs and terminality',
    () async {
      final dir = await Directory.systemTemp.createTemp(
        'coinpup-history-test-',
      );
      addTearDown(() => dir.delete(recursive: true));
      final file = path.join(dir.path, 'offline.sqlite');
      CachedOperation state(
        String id,
        Map<String, Object> posting, {
        bool removed = false,
      }) => CachedOperation(
        ledgerId: 'ledger-a',
        id: id,
        version: removed ? 2 : 1,
        removed: removed,
        json: jsonEncode({
          'id': id,
          'ledger_id': 'ledger-a',
          'kind': posting['kind'],
          'version': removed ? 2 : 1,
          'status': removed ? 'cancelled' : 'active',
          'updated_at': '2026-11-25T12:00:00Z',
          'latest_posting': {
            'id': id,
            'ledger_id': 'ledger-a',
            'journal_id': 'journal-$id',
            'version': 1,
            'transaction_date': '2026-11-20',
            'recognition_date': '2026-11-20',
            'description': 'Fictional posting',
            'created_at': '2026-11-24T12:00:00Z',
            ...posting,
          },
          if (removed)
            'cancellation': {
              'version': 2,
              'reason': 'Fictional cancellation',
              'reversal_journal_id': 'reversal-$id',
              'recorded_at': '2026-11-25T12:00:00Z',
            },
        }),
      );
      const opening = {
        'kind': 'opening',
        'account_id': 'bank',
        'asset_id': 'USD',
        'amount': '-90071992547409.03',
        'splits': [],
      };
      const transfer = {
        'kind': 'transfer',
        'source_account_id': 'bank',
        'destination_account_id': 'archived-bank',
        'asset_id': 'USD',
        'amount': '15.01',
      };
      const exchange = {
        'kind': 'exchange',
        'source_account_id': 'bank',
        'source_asset_id': 'token:chain-a:FictionalUnit',
        'source_amount': '1.000000000000000001',
        'destination_account_id': 'bank',
        'destination_asset_id': 'token:chain-b:FictionalUnit',
        'destination_amount': '0.999999999999999998',
        'recognition_date': '2026-11-22',
        'fees': [
          {
            'account_id': 'archived-bank',
            'asset_id': 'USD',
            'amount': '0.01',
            'category_id': 'wire-fee',
          },
        ],
      };
      final first = await OfflineStore.open(databaseFactoryFfi, file, scope);
      await first.put(
        'sync',
        'catalog',
        jsonEncode({
          'assets': [],
          'ledgers': [
            {
              'id': 'ledger-a',
              'name': 'Fictional ledger',
              'archived': false,
              'accounts': [
                {'id': 'archived-bank', 'name': 'Older bank', 'archived': true},
              ],
              'categories': [],
            },
          ],
        }),
      );
      await first.commitWindow(
        after: '0',
        delivered: '4',
        witness: 'fixture-page',
        changes: [
          state('opening', opening),
          state('transfer', transfer),
          state('exchange', exchange),
          state('cancelled', transfer, removed: true),
        ],
      );
      await first.close();

      final reopened = await OfflineStore.open(databaseFactoryFfi, file, scope);
      addTearDown(reopened.close);
      final cached = await MobileWorkspace(reopened)
          .snapshot('Fictional owner', SyncStatus.offline);
      final records = {
        for (final row in cached.readOnlyPostings) row.kind: row,
      };
      expect(
        cached.readOnlyPostings.map((row) => row.id),
        unorderedEquals(['opening', 'transfer', 'exchange']),
      );
      expect(cached.entries, isEmpty);
      expect(cached.ledgers.single.accounts, isEmpty);
      expect(cached.ledgers.single.archivedAccountNames, {
        'archived-bank': 'Older bank',
      });
      expect(
        records[ReadOnlyPostingKind.opening]!.primary.amount,
        '-90071992547409.03',
      );
      expect(records[ReadOnlyPostingKind.opening]!.destination, isNull);
      final movement = records[ReadOnlyPostingKind.transfer]!;
      expect(movement.primary, (
        accountId: 'bank',
        assetId: 'USD',
        amount: '15.01',
      ));
      expect(movement.destination, (
        accountId: 'archived-bank',
        assetId: 'USD',
        amount: '15.01',
      ));
      final fx = records[ReadOnlyPostingKind.exchange]!;
      expect(fx.primary, (
        accountId: 'bank',
        assetId: 'token:chain-a:FictionalUnit',
        amount: '1.000000000000000001',
      ));
      expect(fx.destination, (
        accountId: 'bank',
        assetId: 'token:chain-b:FictionalUnit',
        amount: '0.999999999999999998',
      ));
      expect(fx.recognitionDate, '2026-11-22');
      expect(fx.fees.single, (
        quantity: (accountId: 'archived-bank', assetId: 'USD', amount: '0.01'),
        categoryId: 'wire-fee',
      ));
      expect(await reopened.operationStamp('ledger-a', 'cancelled'), (
        version: 2,
        removed: true,
      ));
    },
  );
}
