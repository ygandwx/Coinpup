import 'dart:io';

import 'package:coinpup_mobile/offline_store.dart';
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
}
