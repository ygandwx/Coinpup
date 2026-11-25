import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:path/path.dart' as paths;
import 'package:path_provider/path_provider.dart';
import 'package:sqflite/sqflite.dart';

import 'exact_amount.dart';
import 'mobile_http.dart';
import 'mobile_sync_feed.dart';
import 'mobile_workspace.dart';
import 'offline_store.dart';
import 'ui/mobile_adapter.dart';

MobileAdapter createMobileAdapter() => CoinpupMobile(
  database: databaseFactory,
  directory: getApplicationSupportDirectory,
  vault: const FlutterSecureStorage(),
);

final class CoinpupMobile implements MobileAdapter {
  CoinpupMobile({
    required this.database,
    required this.directory,
    required this.vault,
  });

  final DatabaseFactory database;
  final Future<Directory> Function() directory;
  final FlutterSecureStorage vault;
  static const _vaultKey = 'coinpup.active-session.v1';
  _MobileSession? _active;
  Future<void> _tail = Future.value();

  Future<T> _serial<T>(Future<T> Function() operation) {
    final result = _tail.then((_) => operation());
    _tail = result.then((_) {}, onError: (Object _, StackTrace _) {});
    return result;
  }

  @override
  Future<MobileSnapshot?> restore() => _serial(() async {
    if (_active != null) return _active!.snapshot();
    final saved = await vault.read(key: _vaultKey);
    if (saved == null) return null;
    final data = readJson(saved);
    final http = MobileHttp(
      validatedMobileServer(Uri.parse(data['server'] as String)),
    );
    http.sessionCookie = data['cookie'] as String;
    http.csrf = data['csrf'] as String;
    try {
      _active = await _open(http, readObject(data['user']));
    } catch (_) {
      http.close();
      rethrow;
    }
    if ((await _active!.store.records('sync'))['require_signin'] == 'true') {
      return _requireSignIn(_active!);
    }
    return _sync(_active!);
  });

  @override
  Future<MobileSnapshot> signIn(Uri server, String username, String password) =>
      _serial(() async {
        final http = MobileHttp(validatedMobileServer(server));
        _MobileSession? next;
        try {
          final result = readObject(
            await http.request(
              'POST',
              '/auth/login',
              json: {'username': username, 'password': password},
            ),
          );
          final user = readObject(result['user']);
          final cookie = http.sessionCookie;
          if (cookie == null) throw const MobileFailure(MobileErrorCode.signIn);
          http.csrf = result['csrf_token'] as String;
          await _active?.close();
          _active = null;
          next = await _open(http, user);
          await vault.write(
            key: _vaultKey,
            value: jsonEncode({
              'server': http.server.origin,
              'user': user,
              'cookie': cookie,
              'csrf': http.csrf,
            }),
          );
          await next.store.remove('sync', 'require_signin');
        } on MobileHttpError {
          http.close();
          throw const MobileFailure(MobileErrorCode.signIn);
        } on IOException {
          http.close();
          throw const MobileFailure(MobileErrorCode.connection);
        } on TimeoutException {
          http.close();
          throw const MobileFailure(MobileErrorCode.connection);
        } catch (_) {
          if (next == null) {
            http.close();
          } else {
            await next.close();
          }
          rethrow;
        }
        _active = next;
        return _sync(next);
      });

  Future<_MobileSession> _open(MobileHttp http, Json user) async {
    final private = await directory();
    await private.create(recursive: true);
    final owner = user['id'] as String;
    final scope = OfflineScope(http.server.origin, owner);
    final store = await OfflineStore.open(
      database,
      paths.join(private.path, 'workspace.sqlite'),
      scope,
    );
    try {
      final saved = await store.records('sync');
      final device = saved['device'] ?? newMobileId();
      if (saved['device'] == null) await store.put('sync', 'device', device);
      return _MobileSession(
        http,
        store,
        device,
        owner,
        user['username'] as String,
      );
    } catch (_) {
      await store.close();
      rethrow;
    }
  }

  @override
  Future<void> signOut() => _serial(() async {
    final session = _active;
    await vault.delete(key: _vaultKey);
    _active = null;
    if (session == null) return;
    try {
      await session.http.request('POST', '/auth/logout');
    } on MobileHttpError {
      // The server may have already expired the authenticated session.
    } on IOException {
      // The offline app is signed out when its local credential is removed.
    } on TimeoutException {
      // A later sign-in creates a different session.
    } finally {
      await session.close();
    }
  });

  Future<void> close() => _serial(() async {
    await _active?.close();
    _active = null;
  });

  _MobileSession get _session =>
      _active ?? (throw const MobileFailure(MobileErrorCode.signIn));

  @override
  Future<MobileSnapshot> save(MobileDraft draft, {MobileEntry? previous}) =>
      _serial(() async {
        final session = _session;
        final view = await session.snapshot();
        final ledger = view.ledgers
            .where((row) => row.id == draft.ledgerId && !row.archived)
            .firstOrNull;
        final asset = view.assets
            .where((row) => row.id == draft.assetId)
            .firstOrNull;
        final validSelections =
            ledger != null &&
            ledger.accounts.any(
              (row) =>
                  row.id == draft.accountId &&
                  row.assetIds.contains(draft.assetId),
            ) &&
            ledger.categories.any(
              (row) => row.id == draft.categoryId && row.kind == draft.kind,
            );
        if (asset == null ||
            !validSelections ||
            (previous != null &&
                (previous.draft.ledgerId != draft.ledgerId ||
                    previous.draft.kind != draft.kind))) {
          throw const MobileFailure(MobileErrorCode.operation);
        }
        if (previous != null) {
          final current = _current(view, previous);
          if (!current.canEdit) {
            throw const MobileFailure(MobileErrorCode.operation);
          }
        }
        final parsed = ExactAmount.parse(draft.amount, asset.id, asset.scale);
        final date = DateTime.tryParse(draft.date);
        if (parsed.units <= BigInt.zero ||
            date == null ||
            date.toIso8601String().substring(0, 10) != draft.date ||
            draft.description.length > 1000) {
          throw const MobileFailure(MobileErrorCode.operation);
        }
        final id = previous?.id ?? newMobileId();
        await _stage(session, id, draft, previous?.version ?? 0);
        return session.snapshot();
      });

  @override
  Future<MobileSnapshot> delete(MobileEntry entry) => _serial(() async {
    final session = _session;
    final current = _current(await session.snapshot(), entry);
    if (!current.canDelete) {
      throw const MobileFailure(MobileErrorCode.operation);
    }
    if (current.version == 0) {
      await session.store.remove(
        'draft',
        localEntryKey(current.draft.ledgerId, current.id),
      );
    } else {
      await _stage(
        session,
        current.id,
        current.draft,
        current.version,
        cancel: true,
      );
    }
    return session.snapshot();
  });

  static MobileEntry _current(MobileSnapshot view, MobileEntry original) =>
      view.entries
          .where(
            (row) =>
                row.id == original.id &&
                row.draft.ledgerId == original.draft.ledgerId &&
                row.version == original.version,
          )
          .firstOrNull ??
      (throw const MobileFailure(MobileErrorCode.operation));

  static Json _draftState(
    String id,
    MobileDraft draft,
    int version, {
    bool cancel = false,
  }) => {
    'operation_id': id,
    'draft': draftJson(draft),
    'version': version,
    'cancel': cancel,
  };

  static Future<void> _stage(
    _MobileSession session,
    String id,
    MobileDraft draft,
    int version, {
    bool cancel = false,
  }) => session.store.put(
    'draft',
    localEntryKey(draft.ledgerId, id),
    jsonEncode(_draftState(id, draft, version, cancel: cancel)),
  );

  @override
  Future<MobileSnapshot> synchronize() => _serial(() => _sync(_session));

  Future<MobileSnapshot> _sync(_MobileSession session) async {
    try {
      final authorization = readObject(
        await session.http.request('GET', '/auth/session'),
      );
      if (readObject(authorization['user'])['id'] != session.owner) {
        return await _requireSignIn(session);
      }
      session.http.csrf = authorization['csrf_token'] as String;
      await session.feed.register();
      // Reuse frozen envelopes before acknowledgements can advance their server checkpoint.
      await _replay(session);
      await session.feed.pull();
      await session.workspace.refresh(session.http);
      await _resolvePending(session);
      for (final item in (await session.store.records('draft')).entries) {
        final staged = readJson(item.value);
        if (staged['requires_edit'] == true) continue;
        await _freeze(session, item.key, staged);
        await _replay(session);
      }
      await session.feed.pull();
      await session.store.put(
        'sync',
        'last_synced',
        DateTime.now().toUtc().toIso8601String(),
      );
      return await session.snapshot(SyncStatus.synced);
    } on MobileHttpError catch (error) {
      if (error.status == 401 || error.code == 'csrf_failed') {
        return _requireSignIn(session);
      }
      if (const {
        'checkpoint_fork',
        'checkpoint_ahead',
        'sync_checkpoint_conflict',
        'device_retired',
        'sync_device_retired',
      }.contains(error.code)) {
        return session.snapshot(SyncStatus.reviewRequired);
      }
      if (error.status >= 500 || error.status == 429 || error.status == 408) {
        return session.snapshot(SyncStatus.offline);
      }
      throw const MobileFailure(MobileErrorCode.sync);
    } on IOException {
      return session.snapshot(SyncStatus.offline);
    } on TimeoutException {
      return session.snapshot(SyncStatus.offline);
    }
  }

  Future<MobileSnapshot> _requireSignIn(_MobileSession session) async {
    _active = null;
    try {
      await session.store.put('sync', 'require_signin', 'true');
      await vault.delete(key: _vaultKey);
    } on PlatformException {
      // The local marker still prevents a denied session from restoring its cache.
    } finally {
      await session.close();
    }
    return const MobileSnapshot(
      ownerLabel: '',
      syncStatus: SyncStatus.signInRequired,
    );
  }

  Future<void> _freeze(_MobileSession session, String key, Json state) async {
    final draft = parseDraft(readObject(state['draft']));
    final version = state['version'] as int;
    final operation = state['operation_id'] as String;
    final partial = <String, Object>{
      'account_id': draft.accountId,
      'asset_id': draft.assetId,
      'amount': draft.amount,
      'transaction_date': draft.date,
      'recognition_date': draft.date,
      'description': draft.description,
      'splits': [
        {'category_id': draft.categoryId, 'amount': draft.amount},
      ],
    };
    final command = version == 0
        ? {
            'kind': draft.kind.name,
            'payload': {'id': operation, ...partial},
          }
        : {
            'kind': state['cancel'] == true ? 'cancel' : 'correct',
            'operation_id': operation,
            'payload': {
              'expected_version': version,
              'reason': state['cancel'] == true
                  ? 'Coinpup mobile cancellation'
                  : 'Coinpup mobile correction',
              if (state['cancel'] != true)
                'replacement': {'kind': draft.kind.name, ...partial},
            },
          };
    final id = newMobileId();
    final request = {
      'ledger_id': draft.ledgerId,
      'idempotency_key': id,
      'checkpoint': wireCheckpoint(await session.store.checkpoint()),
      'command': command,
    };
    await session.store.enqueue(
      FrozenIntent(
        id: id,
        ledgerId: draft.ledgerId,
        commandKey: id,
        bodyJson: jsonEncode(request),
      ),
      draftId: key,
    );
  }

  Future<void> _replay(_MobileSession session) async {
    while (true) {
      final intent = await session.store.nextIntent();
      if (intent == null) return;
      await session.store.markAttempted(intent.id);
      final request = readJson(intent.bodyJson);
      final command = readObject(request['command']);
      final payload = readObject(command['payload']);
      final operation = (command['operation_id'] ?? payload['id']) as String;
      final Json result;
      try {
        result = readObject(
          await session.http.request(
            'PUT',
            '${session.feed.devicePath}/financial-pushes/${intent.id}',
            rawJson: intent.bodyJson,
          ),
        );
      } on MobileHttpError catch (error) {
        if (!const {
          'period_closed',
          'entity_archived',
          'account_archived',
          'asset_disabled',
          'account_asset_disabled',
          'category_archived',
          'invalid_financial_push',
        }.contains(error.code)) {
          rethrow;
        }
        final replacement = command['kind'] == 'cancel'
            ? null
            : readObject(
                payload['replacement'] ?? {...payload, 'kind': command['kind']},
              );
        final draft = replacement == null
            ? null
            : draftFromPosting(intent.ledgerId, replacement);
        await session.store.settle(
          intent.id,
          jsonEncode({
            'outcome': 'rejected',
            'operation_id': operation,
            'code': error.code,
          }),
          retainedDraft: draft == null
              ? null
              : (
                  key: localEntryKey(intent.ledgerId, operation),
                  json: jsonEncode({
                    ..._draftState(
                      operation,
                      draft,
                      (payload['expected_version'] ?? 0) as int,
                    ),
                    'requires_edit': true,
                  }),
                ),
        );
        continue;
      }
      if (result['intent_id'] != intent.id ||
          result['device_id'] != session.device ||
          result['ledger_id'] != intent.ledgerId ||
          result['operation_id'] != operation ||
          !{'conflict', 'applied'}.contains(result['outcome'])) {
        throw const FormatException(
          'Financial push receipt does not match original intent',
        );
      }
      await session.store.settle(intent.id, jsonEncode(result));
    }
  }

  @override
  Future<MobileSnapshot> resolveConflict(
    String id,
    ConflictChoice choice,
  ) => _serial(() async {
    final session = _session;
    final pending = await session.store.records('decision');
    if (pending.containsKey(id)) return _sync(session);
    final view = await session.snapshot();
    final conflict = view.conflicts.where((row) => row.id == id).firstOrNull;
    final version = conflict?.latestVersion ?? conflict?.currentVersion;
    if (conflict == null ||
        version == null ||
        (choice == ConflictChoice.submitLocal &&
            (!conflict.canKeepLocal || conflict.latestDeleted))) {
      throw const MobileFailure(MobileErrorCode.conflict);
    }
    final result = (await session.store.intentions())
        .firstWhere((intent) => intent.id == id)
        .result!;
    final operation = readJson(result)['operation_id'] as String;
    final draft = choice == ConflictChoice.submitLocal
        ? _draftState(operation, conflict.submitted!, version)
        : null;
    await session.store.put(
      'decision',
      id,
      jsonEncode({'expected_current_version': version, 'draft': draft}),
    );
    try {
      await _resolvePending(session);
      return await session.snapshot();
    } on MobileHttpError catch (error) {
      if (error.status == 401 || error.code == 'csrf_failed') {
        return _requireSignIn(session);
      }
      if (error.status >= 500 || error.status == 429 || error.status == 408) {
        return session.snapshot(SyncStatus.offline);
      }
      throw const MobileFailure(MobileErrorCode.conflict);
    } on IOException {
      return session.snapshot(SyncStatus.offline);
    } on TimeoutException {
      return session.snapshot(SyncStatus.offline);
    }
  });

  Future<void> _resolvePending(_MobileSession session) async {
    for (final item in (await session.store.records('decision')).entries) {
      final decision = readJson(item.value);
      final version = decision['expected_current_version'] as int;
      try {
        final receipt = readObject(
          await session.http.request(
            'PUT',
            '${session.feed.devicePath}/financial-pushes/${item.key}/resolution',
            json: {'expected_current_version': version},
          ),
        );
        if (receipt['intent_id'] != item.key ||
            receipt['device_id'] != session.device ||
            receipt['accepted_version'] != version) {
          throw const MobileFailure(MobileErrorCode.conflict);
        }
        final staged = decision['draft'] == null
            ? null
            : readObject(decision['draft']);
        final next = staged == null
            ? null
            : parseDraft(readObject(staged['draft']));
        await session.store.resolveConflict(
          item.key,
          draft: staged == null
              ? null
              : (
                  key: localEntryKey(
                    next!.ledgerId,
                    staged['operation_id'] as String,
                  ),
                  json: jsonEncode(staged),
                ),
        );
      } on MobileHttpError catch (error) {
        if (error.code != 'version_conflict') rethrow;
        // This decision never committed. Retain the original conflict for another choice.
        await session.store.remove('decision', item.key);
      }
    }
  }
}

final class _MobileSession {
  _MobileSession(this.http, this.store, this.device, this.owner, this.label)
    : workspace = MobileWorkspace(store),
      feed = MobileSyncFeed(http, store, device);

  final MobileHttp http;
  final OfflineStore store;
  final String device;
  final String owner;
  final String label;
  final MobileWorkspace workspace;
  final MobileSyncFeed feed;

  Future<MobileSnapshot> snapshot([SyncStatus status = SyncStatus.ready]) =>
      workspace.snapshot(label, status);

  Future<void> close() async {
    http.close();
    await store.close();
  }
}
