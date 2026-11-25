import 'package:sqflite/sqflite.dart';

final class OfflineScope {
  const OfflineScope(this.authority, this.ownerId);

  final String authority;
  final String ownerId;

  List<String> get keys => [authority, ownerId];
}

final class CachedOperation {
  const CachedOperation({
    required this.ledgerId,
    required this.id,
    required this.version,
    required this.json,
    this.removed = false,
  });

  final String ledgerId;
  final String id;
  final int version;
  final String json;
  final bool removed;
}

final class FrozenIntent {
  const FrozenIntent({
    required this.id,
    required this.ledgerId,
    required this.commandKey,
    required this.bodyJson,
    this.attempted = false,
  });

  final String id;
  final String ledgerId;
  final String commandKey;
  final String bodyJson;
  final bool attempted;
}

final class OfflineStore {
  OfflineStore._(this._db, this.scope);

  static Future<OfflineStore> open(
    DatabaseFactory factory,
    String path,
    OfflineScope scope,
  ) async {
    final db = await factory.openDatabase(
      path,
      options: OpenDatabaseOptions(
        version: 2,
        onUpgrade: (db, previous, _) async {
          if (previous == 1) await _createWorkspace(db);
        },
        onCreate: (db, _) async {
          await db.execute('''
          CREATE TABLE operations (
            authority TEXT NOT NULL, owner_id TEXT NOT NULL,
            ledger_id TEXT NOT NULL, id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version > 0),
            removed INTEGER NOT NULL CHECK(removed IN (0,1)),
            body_json TEXT NOT NULL,
            PRIMARY KEY(authority, owner_id, ledger_id, id)
          )
        ''');
          await db.execute('''
          CREATE TABLE checkpoints (
            authority TEXT NOT NULL, owner_id TEXT NOT NULL,
            seq TEXT NOT NULL, witness TEXT NOT NULL,
            PRIMARY KEY(authority, owner_id)
          )
        ''');
          await db.execute('''
          CREATE TABLE outbox (
            slot INTEGER PRIMARY KEY, authority TEXT NOT NULL,
            owner_id TEXT NOT NULL, intent_id TEXT NOT NULL,
            ledger_id TEXT NOT NULL, command_key TEXT NOT NULL, body_json TEXT NOT NULL,
            attempted INTEGER NOT NULL DEFAULT 0 CHECK(attempted IN (0,1)),
            outcome_json TEXT,
            UNIQUE(authority, owner_id, intent_id),
            UNIQUE(authority, owner_id, ledger_id, command_key)
          )
        ''');
          await _createWorkspace(db);
        },
      ),
    );
    return OfflineStore._(db, scope);
  }

  static Future<void> _createWorkspace(DatabaseExecutor db) => db.execute('''
    CREATE TABLE workspace (
      authority TEXT NOT NULL, owner_id TEXT NOT NULL,
      namespace TEXT NOT NULL, id TEXT NOT NULL, body_json TEXT NOT NULL,
      PRIMARY KEY(authority, owner_id, namespace, id)
    )
  ''');

  final Database _db;
  final OfflineScope scope;
  static const _owner = 'authority = ? AND owner_id = ?';
  static const _entry = '$_owner AND ledger_id = ? AND id = ?';
  static const _intent = '$_owner AND intent_id = ?';
  static const _workspace = '$_owner AND namespace = ?';

  Map<String, String> get _scopeRow => {
    'authority': scope.authority,
    'owner_id': scope.ownerId,
  };

  Future<void> close() => _db.close();

  Future<String?> record(String namespace, String id) async {
    final rows = await _db.query(
      'workspace',
      columns: ['body_json'],
      where: '$_workspace AND id = ?',
      whereArgs: [...scope.keys, namespace, id],
    );
    return rows.firstOrNull?['body_json'] as String?;
  }

  Future<Map<String, String>> records(String namespace) async {
    final rows = await _db.query(
      'workspace',
      where: _workspace,
      whereArgs: [...scope.keys, namespace],
      orderBy: 'rowid',
    );
    return {
      for (final row in rows) row['id'] as String: row['body_json'] as String,
    };
  }

  Future<void> _writeRecord(
    DatabaseExecutor db,
    String namespace,
    String id,
    String body,
  ) async {
    await db.insert('workspace', {
      ..._scopeRow,
      'namespace': namespace,
      'id': id,
      'body_json': body,
    }, conflictAlgorithm: ConflictAlgorithm.replace);
  }

  Future<void> _deleteRecord(
    DatabaseExecutor db,
    String namespace,
    String id,
  ) => db.delete(
    'workspace',
    where: '$_workspace AND id = ?',
    whereArgs: [...scope.keys, namespace, id],
  );

  Future<void> put(String namespace, String id, String body) =>
      _writeRecord(_db, namespace, id, body);

  Future<void> remove(String namespace, String id) =>
      _deleteRecord(_db, namespace, id);

  Future<void> resolveConflict(
    String id, {
    ({String key, String json})? draft,
  }) => _db.transaction((db) async {
    if (draft != null) await _writeRecord(db, 'draft', draft.key, draft.json);
    await _writeRecord(db, 'resolution', id, 'true');
    await _deleteRecord(db, 'decision', id);
  });

  Future<List<CachedOperation>> operations(String ledgerId) async {
    final rows = await _db.query(
      'operations',
      where: '$_owner AND ledger_id = ? AND removed = 0',
      whereArgs: [...scope.keys, ledgerId],
      orderBy: 'id',
    );
    return [
      for (final row in rows)
        CachedOperation(
          ledgerId: ledgerId,
          id: row['id'] as String,
          version: row['version'] as int,
          json: row['body_json'] as String,
        ),
    ];
  }

  Future<({int version, bool removed})?> operationStamp(
    String ledgerId,
    String id,
  ) async {
    final rows = await _db.query(
      'operations',
      columns: ['version', 'removed'],
      where: _entry,
      whereArgs: [...scope.keys, ledgerId, id],
    );
    final row = rows.firstOrNull;
    return row == null
        ? null
        : (version: row['version'] as int, removed: row['removed'] == 1);
  }

  Future<({String seq, String witness})> checkpoint() => _checkpoint(_db);

  Future<({String seq, String witness})> _checkpoint(
    DatabaseExecutor db,
  ) async {
    final rows = await db.query(
      'checkpoints',
      where: _owner,
      whereArgs: scope.keys,
    );
    return rows.isEmpty
        ? (seq: '0', witness: '')
        : (
            seq: rows.single['seq'] as String,
            witness: rows.single['witness'] as String,
          );
  }

  /// Persist a completely obtained window before sending its server acknowledgement.
  Future<void> commitWindow({
    required String after,
    required String delivered,
    required String witness,
    required List<CachedOperation> changes,
    String? acknowledgement,
  }) => _db.transaction((db) async {
    final current = await _checkpoint(db);
    if (after != current.seq ||
        BigInt.parse(delivered) < BigInt.parse(after) ||
        (delivered != after && witness.isEmpty)) {
      throw StateError('Invalid or stale change window');
    }
    for (final change in changes) {
      final keys = [...scope.keys, change.ledgerId, change.id];
      final old = await db.query('operations', where: _entry, whereArgs: keys);
      if (old.isNotEmpty) {
        final entry = old.single;
        if (entry['removed'] == 1 && !change.removed) continue;
        final version = entry['version'] as int;
        if (change.version < version) continue;
        if (change.version == version) {
          if (entry['removed'] != (change.removed ? 1 : 0) ||
              entry['body_json'] != change.json) {
            throw StateError('Different snapshots for the same version');
          }
          continue;
        }
      }
      await db.insert('operations', {
        ..._scopeRow,
        'ledger_id': change.ledgerId,
        'id': change.id,
        'version': change.version,
        'removed': change.removed ? 1 : 0,
        'body_json': change.json,
      }, conflictAlgorithm: ConflictAlgorithm.replace);
    }
    await db.insert('checkpoints', {
      ..._scopeRow,
      'seq': delivered,
      'witness': witness,
    }, conflictAlgorithm: ConflictAlgorithm.replace);
    if (acknowledgement != null) {
      await _writeRecord(db, 'sync', 'ack', acknowledgement);
    }
  });

  Future<void> enqueue(FrozenIntent intent, {String? draftId}) =>
      _db.transaction((db) async {
        final existing = await db.query(
          'outbox',
          where: _intent,
          whereArgs: [...scope.keys, intent.id],
        );
        if (existing.isNotEmpty) {
          final saved = existing.single;
          if (saved['ledger_id'] != intent.ledgerId ||
              saved['command_key'] != intent.commandKey ||
              saved['body_json'] != intent.bodyJson) {
            throw StateError('An original intent cannot be rewritten');
          }
          return;
        }
        await db.insert('outbox', {
          ..._scopeRow,
          'intent_id': intent.id,
          'ledger_id': intent.ledgerId,
          'command_key': intent.commandKey,
          'body_json': intent.bodyJson,
        });
        if (draftId != null) await _deleteRecord(db, 'draft', draftId);
      });

  Future<FrozenIntent?> nextIntent() async {
    final rows = await _db.query(
      'outbox',
      where: '$_owner AND outcome_json IS NULL',
      whereArgs: scope.keys,
      orderBy: 'slot',
      limit: 1,
    );
    if (rows.isEmpty) return null;
    final row = rows.single;
    return FrozenIntent(
      id: row['intent_id'] as String,
      ledgerId: row['ledger_id'] as String,
      commandKey: row['command_key'] as String,
      bodyJson: row['body_json'] as String,
      attempted: row['attempted'] == 1,
    );
  }

  Future<void> markAttempted(String id) async {
    final updated = await _db.update(
      'outbox',
      {'attempted': 1},
      where: '$_intent AND outcome_json IS NULL',
      whereArgs: [...scope.keys, id],
    );
    if (updated != 1) throw StateError('Unknown or settled intent');
  }

  Future<List<({String id, String body, String? result})>> intentions() async {
    final rows = await _db.query(
      'outbox',
      where: _owner,
      whereArgs: scope.keys,
      orderBy: 'slot',
    );
    return [
      for (final row in rows)
        (
          id: row['intent_id'] as String,
          body: row['body_json'] as String,
          result: row['outcome_json'] as String?,
        ),
    ];
  }

  Future<void> settle(
    String id,
    String outcomeJson, {
    ({String key, String json})? retainedDraft,
  }) => _db.transaction((db) async {
    final rows = await db.query(
      'outbox',
      where: _intent,
      whereArgs: [...scope.keys, id],
    );
    if (rows.length != 1 || rows.single['attempted'] != 1) {
      throw StateError('Unknown original attempt');
    }
    final previous = rows.single['outcome_json'];
    if (previous == outcomeJson) return;
    if (previous != null) {
      throw StateError('An original outcome cannot be replaced');
    }
    await db.update(
      'outbox',
      {'outcome_json': outcomeJson},
      where: _intent,
      whereArgs: [...scope.keys, id],
    );
    if (retainedDraft != null) {
      await _writeRecord(db, 'draft', retainedDraft.key, retainedDraft.json);
    }
  });
}
