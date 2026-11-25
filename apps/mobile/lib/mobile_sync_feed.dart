import 'dart:convert';

import 'package:flutter/foundation.dart';

import 'mobile_http.dart';
import 'offline_store.dart';

Json readJson(String body) => readObject(jsonDecode(body));

Json wireCheckpoint(({String seq, String witness}) local) => {
  'cursor': local.seq,
  'witness': local.witness.isEmpty ? null : readJson(local.witness),
};

final class MobileSyncFeed {
  MobileSyncFeed(this.http, this.store, this.deviceId);

  final MobileHttp http;
  final OfflineStore store;
  final String deviceId;

  String get devicePath => '/sync/devices/$deviceId';

  Future<void> register() async {
    final registered = readObject(
      await http.request(
        'PUT',
        devicePath,
        json: {'cursor': '0', 'witness': null},
      ),
    );
    if (registered['state'] != 'registered') {
      throw const MobileHttpError(409, 'device_retired');
    }
  }

  Future<void> pull() async {
    await _acknowledge();
    final saved = readObject(
      await http.request('GET', '$devicePath/checkpoint'),
    );
    final localBefore = await store.checkpoint();
    final localWitness = wireCheckpoint(localBefore)['witness'] as Json?;
    final remoteWitness = saved['witness'] == null
        ? null
        : readObject(saved['witness']);
    if (saved['cursor'] != localBefore.seq ||
        !mapEquals(remoteWitness, localWitness)) {
      throw const MobileHttpError(409, 'checkpoint_fork');
    }
    final bound =
        readObject(await http.request('GET', '/changes/start'))['cursor']
            as String;
    while (true) {
      final local = await store.checkpoint();
      if (local.seq == bound) return;
      if (BigInt.parse(local.seq) > BigInt.parse(bound)) {
        throw const MobileHttpError(409, 'checkpoint_ahead');
      }
      const limit = 100;
      final page = readObject(
        await http.request(
          'GET',
          '/changes',
          query: {'after': local.seq, 'through': bound, 'limit': '$limit'},
        ),
      );
      final changes = readRows(page['changes']);
      final delivered = page['next_cursor'] as String;
      if (changes.isEmpty ||
          BigInt.parse(delivered) <= BigInt.parse(local.seq) ||
          BigInt.parse(delivered) > BigInt.parse(bound) ||
          changes.last['seq'] != delivered) {
        throw const FormatException('Change page did not advance');
      }
      final operations = <CachedOperation>[];
      final visited = <String>{};
      for (final notice in changes.reversed) {
        final ledger = notice['ledger_id'] as String?;
        final id = notice['entity_id'] as String;
        if (notice['entity_type'] != 'financial_operations' ||
            ledger == null ||
            !visited.add('$ledger/$id')) {
          continue;
        }
        final projection = readObject(
          await http.request(
            'GET',
            '/ledgers/$ledger/operations/$id/sync-projection',
          ),
        );
        final version = projection['version'] as int;
        final status = projection['status'];
        if (projection['id'] != id ||
            projection['ledger_id'] != ledger ||
            !{'active', 'cancelled'}.contains(status) ||
            version < (notice['entity_version'] as int)) {
          throw const FormatException(
            'Operation does not match its change notice',
          );
        }
        final posting = projection['posting'];
        if (posting == null) {
          if (await store.operationStamp(ledger, id) != null) {
            throw const FormatException(
              'An existing cash operation lost its projection',
            );
          }
          continue;
        }
        final operation = readObject(posting);
        if (operation['id'] != id ||
            operation['ledger_id'] != ledger ||
            operation['version'] != version ||
            operation['status'] != status) {
          throw const FormatException(
            'Cash operation differs from its own projection',
          );
        }
        operations.add(
          CachedOperation(
            ledgerId: ledger,
            id: id,
            version: version,
            json: jsonEncode(operation),
            removed: status == 'cancelled',
          ),
        );
      }
      final ack = {
        'after': local.seq,
        'limit': limit,
        'prior': wireCheckpoint(local)['witness'],
        'received': page,
        'through': bound,
      };
      await store.commitWindow(
        after: local.seq,
        delivered: delivered,
        witness: jsonEncode(changes.last),
        changes: operations,
        acknowledgement: jsonEncode(ack),
      );
      await _acknowledge();
    }
  }

  Future<void> _acknowledge() async {
    final pending = (await store.records('sync'))['ack'];
    if (pending == null) return;
    final receipt = readObject(
      await http.request(
        'POST',
        '$devicePath/acknowledgements',
        rawJson: pending,
      ),
    );
    final saved = readJson(pending);
    final received = readObject(saved['received']);
    if (receipt['device_id'] != deviceId ||
        receipt['owner_id'] != store.scope.ownerId ||
        receipt['after'] != saved['after'] ||
        receipt['through'] != saved['through'] ||
        receipt['limit'] != saved['limit'] ||
        receipt['count'] != readRows(received['changes']).length ||
        receipt['cursor'] != received['next_cursor']) {
      throw const FormatException('Unexpected change acknowledgement');
    }
    await store.remove('sync', 'ack');
  }
}
