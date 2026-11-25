import 'dart:convert';

import 'mobile_http.dart';
import 'mobile_sync_feed.dart';
import 'offline_store.dart';
import 'ui/mobile_adapter.dart';

Json draftJson(MobileDraft draft) => {
  'ledger_id': draft.ledgerId,
  'kind': draft.kind.name,
  'account_id': draft.accountId,
  'category_id': draft.categoryId,
  'asset_id': draft.assetId,
  'amount': draft.amount,
  'date': draft.date,
  'description': draft.description,
};

String localEntryKey(String ledger, String operation) => '$ledger/$operation';

MobileDraft parseDraft(Json row) => MobileDraft(
  ledgerId: row['ledger_id'] as String,
  kind: EntryKind.values.byName(row['kind'] as String),
  accountId: row['account_id'] as String,
  categoryId: row['category_id'] as String,
  assetId: row['asset_id'] as String,
  amount: row['amount'] as String,
  date: row['date'] as String,
  description: row['description'] as String,
);

MobileDraft? draftFromPosting(String ledger, Json posting) {
  final kind = posting['kind'];
  if (kind != 'income' && kind != 'expense') return null;
  final splits = readRows(posting['splits']);
  if (splits.isEmpty) return null;
  return parseDraft({
    ...posting,
    'ledger_id': ledger,
    'date': posting['transaction_date'],
    'category_id': splits.first['category_id'],
  });
}

bool isSimplePosting(Json posting) =>
    readRows(posting['splits']).length == 1 &&
    readRows(posting['fees'] ?? []).isEmpty &&
    posting['transaction_date'] == posting['recognition_date'];

MobileDraft? draftFromHistory(String ledger, EntryKind kind, Json history) {
  if (history['action'] == 'cancel') return null;
  for (final journal in readRows(history['journals'])) {
    if (journal['kind'] != 'posting') continue;
    final lines = readRows(journal['lines']);
    final accountLines = lines
        .where((line) => line['account_id'] != null)
        .toList();
    final categoryLines = lines
        .where((line) => line['category_id'] != null)
        .toList();
    if (accountLines.length != 1 || categoryLines.length != 1) return null;
    final account = accountLines.single;
    final quantity = (account['amount'] as String).replaceFirst(
      RegExp('^-'),
      '',
    );
    return parseDraft({
      'ledger_id': ledger,
      'kind': kind.name,
      'asset_id': account['asset_id'],
      'account_id': account['account_id'],
      'amount': quantity,
      'category_id': categoryLines.single['category_id'],
      'date': journal['transaction_date'],
      'description': journal['description'],
    });
  }
  return null;
}

final class MobileWorkspace {
  const MobileWorkspace(this.store);

  final OfflineStore store;

  Future<void> refresh(MobileHttp http) async {
    const archived = {'include_archived': 'true'};
    final assets = await http.list('/assets');
    final entities = await http.list('/entities', filters: archived);
    final ledgers = <Json>[];
    for (final entity in entities) {
      final ledger = readObject(entity['ledger']);
      final id = ledger['id'];
      final accounts = await http.list(
        '/ledgers/$id/accounts',
        filters: archived,
      );
      final categories = await http.list(
        '/ledgers/$id/categories',
        filters: archived,
      );
      ledgers.add({
        ...ledger,
        'name': entity['name'],
        'archived': entity['archived'],
        'accounts': accounts,
        'categories': categories,
      });
    }
    await store.put(
      'sync',
      'catalog',
      jsonEncode({'assets': assets, 'ledgers': ledgers}),
    );
  }

  Future<MobileSnapshot> snapshot(String label, SyncStatus status) async {
    final metadata = await store.records('sync');
    final catalog = metadata['catalog'] == null
        ? null
        : readJson(metadata['catalog']!);
    final draftRows = await store.records('draft');
    final resolutions = await store.records('resolution');
    final intents = await store.intentions();
    final entries = <String, MobileEntry>{};
    final conflicts = <MobileConflict>[];
    final busy = <String>{};
    final ledgers = catalog == null ? <Json>[] : readRows(catalog['ledgers']);
    for (final ledger in ledgers) {
      final ledgerId = ledger['id'] as String;
      for (final cached in await store.operations(ledgerId)) {
        final operation = readJson(cached.json);
        final posting = readObject(operation['latest_posting']);
        final draft = draftFromPosting(ledgerId, posting);
        if (draft == null) continue;
        final active = ledger['archived'] != true;
        entries[localEntryKey(ledgerId, cached.id)] = MobileEntry(
          id: cached.id,
          version: cached.version,
          draft: draft,
          lifecycle: EntryLifecycle.posted,
          canEdit: active && isSimplePosting(posting),
          canDelete: active,
        );
      }
    }

    var pending = 0;
    for (final intent in intents) {
      final wire = readJson(intent.body);
      final command = readObject(wire['command']);
      final payload = readObject(command['payload']);
      final id = (command['operation_id'] ?? payload['id']) as String;
      final ledger = wire['ledger_id'] as String;
      final entryKey = localEntryKey(ledger, id);
      if (intent.result == null) {
        busy.add(entryKey);
        pending++;
        final draft = draftFromPosting(
          ledger,
          readObject(
            payload['replacement'] ?? {...payload, 'kind': command['kind']},
          ),
        );
        if (draft != null) {
          entries[entryKey] = MobileEntry(
            id: id,
            version: (payload['expected_version'] ?? 0) as int,
            draft: draft,
            lifecycle: EntryLifecycle.queued,
          );
        } else {
          entries.remove(entryKey);
        }
        continue;
      }
      final outcome = readJson(intent.result!);
      if (outcome['outcome'] == 'applied') {
        final receipt = readObject(outcome['receipt']);
        final version = receipt['version'] as int;
        if (((await store.operationStamp(ledger, id))?.version ?? 0) >=
            version) {
          continue;
        }
        if (receipt['status'] == 'cancelled') {
          entries.remove(entryKey);
          continue;
        }
        final posting = readObject(receipt['latest_posting'] ?? receipt);
        final draft = draftFromPosting(ledger, posting);
        if (draft != null) {
          entries[entryKey] = MobileEntry(
            id: id,
            version: version,
            draft: draft,
            lifecycle: EntryLifecycle.posted,
          );
        }
        continue;
      }
      if (outcome['outcome'] != 'conflict' ||
          resolutions[intent.id] == 'true') {
        continue;
      }
      final basis = readObject(outcome['basis']);
      final competing = readObject(outcome['competing']);
      final posted = entries[entryKey];
      final replacement = payload['replacement'];
      final submitted = replacement == null
          ? null
          : draftFromPosting(ledger, readObject(replacement));
      final kind = submitted?.kind ?? posted?.draft.kind;
      final current = kind == null
          ? null
          : draftFromHistory(ledger, kind, competing);
      final version = competing['version'] as int;
      final stamp = await store.operationStamp(ledger, id);
      final latest = stamp != null && stamp.version > version ? stamp : null;
      conflicts.add(
        MobileConflict(
          id: intent.id,
          ledgerId: ledger,
          submitted:
              submitted ??
              (kind == null ? null : draftFromHistory(ledger, kind, basis)),
          current: current,
          submittedVersion: basis['version'] as int,
          currentVersion: version,
          latest: latest == null ? null : posted?.draft,
          latestVersion: latest?.version,
          serverDeleted: competing['action'] == 'cancel',
          latestDeleted: latest?.removed ?? false,
          canKeepLocal:
              submitted != null &&
              posted?.canEdit == true &&
              posted?.version == (latest?.version ?? version),
        ),
      );
    }
    for (final row in draftRows.entries) {
      final state = readJson(row.value);
      if (state['cancel'] == true) {
        entries.remove(row.key);
        continue;
      }
      final draft = parseDraft(readObject(state['draft']));
      final version = state['version'] as int;
      final operationId = state['operation_id'] as String;
      entries[row.key] = MobileEntry(
        id: operationId,
        version: version,
        draft: draft,
        lifecycle: state['requires_edit'] == true
            ? EntryLifecycle.conflict
            : EntryLifecycle.queued,
        canEdit: !busy.contains(row.key),
        canDelete: !busy.contains(row.key),
      );
    }
    final assets = readRows(catalog?['assets'] ?? []);
    final disabledAssetIds = {
      for (final asset in assets)
        if (asset['enabled'] == false) asset['asset_id'] as String,
    };
    return MobileSnapshot(
      ownerLabel: label,
      ledgers: ledgers.map((row) => _ledger(row, disabledAssetIds)).toList(),
      assets: [
        for (final asset in assets)
          MobileAsset(
            id: asset['asset_id'] as String,
            code: asset['code'] as String,
            scale: asset['scale'] as int,
          ),
      ],
      entries: entries.values.toList()
        ..sort((a, b) => b.draft.date.compareTo(a.draft.date)),
      conflicts: conflicts,
      syncStatus:
          (status == SyncStatus.ready || status == SyncStatus.synced) &&
              pending + draftRows.length > 0
          ? SyncStatus.pending
          : status,
      pendingCount: pending + draftRows.length,
      lastSyncedAt: metadata['last_synced'],
    );
  }

  static MobileLedger _ledger(Json record, Set<String> disabledAssetIds) =>
      MobileLedger(
        id: record['id'] as String,
        name: record['name'] as String,
        archived: record['archived'] as bool,
        accounts: [
          for (final account in readRows(record['accounts']))
            if (account['archived'] == false)
              MobileAccount(
                id: account['id'] as String,
                name: account['name'] as String,
                assetIds: [
                  for (final id
                      in (account['asset_ids'] as List).cast<String>())
                    if (!disabledAssetIds.contains(id)) id,
                ],
              ),
        ],
        archivedAccountNames: {
          for (final account in readRows(record['accounts']))
            if (account['archived'] == true)
              account['id'] as String: account['name'] as String,
        },
        categories: [
          for (final category in readRows(record['categories']))
            if (category['archived'] == false &&
                (category['kind'] == 'income' || category['kind'] == 'expense'))
              MobileCategory(
                id: category['id'] as String,
                name: category['name'] as String,
                kind: EntryKind.values.byName(category['kind'] as String),
              ),
        ],
      );
}
