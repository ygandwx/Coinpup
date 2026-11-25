import 'dart:convert';
import 'dart:io';

import 'package:coinpup_mobile/mobile_runtime.dart';
import 'package:coinpup_mobile/offline_store.dart';
import 'package:coinpup_mobile/ui/mobile_adapter.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

const _owner = 'b3db2874-b033-42a4-87f3-934ebeaf0281';
const _ledger = 'df10b860-317d-41cf-9401-42d29d16787d';
const _bank = '0b888da6-b3d0-44cc-8a2b-5ab8bb3906fb';
const _category = '31fdcfaa-b405-4eec-bc53-b0d7d7d3bc43';
const _intent = '1e4110d0-89d8-4614-a0fc-b472d4c4307d';
const _operation = 'be324006-5a62-4f91-a120-ba83129b766e';
const _user = {'id': _owner, 'username': 'fictional-owner'};
const _anchor = {'cursor': '0', 'witness': null};

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final overrides = HttpOverrides.current;
  late Directory directory;
  late _DraftServer server;
  late CoinpupMobile client;

  CoinpupMobile openMobile() => CoinpupMobile(
    database: databaseFactoryFfi,
    directory: () async => directory,
    vault: const FlutterSecureStorage(),
  );

  setUp(() async {
    HttpOverrides.global = null;
    FlutterSecureStorage.setMockInitialValues({});
    sqfliteFfiInit();
    directory = await Directory.systemTemp.createTemp('coinpup-mobile-reject-');
    server = await _DraftServer.open();
    client = openMobile();
  });

  tearDown(() async {
    await client.close();
    await server.http.close(force: true);
    await directory.delete(recursive: true);
    HttpOverrides.global = overrides;
  });

  test(
    'disabled assets become editable without blocking an uncertain intent',
    () async {
      await client.signIn(server.url, 'fictional-owner', 'fictional');
      await client.save(_draft('USD'));
      await client.save(_draft('CNY'));
      await client.save(_draft('EUR'));

      server.disabled = true;
      server.loseNextReceipt = true;
      expect((await client.synchronize()).syncStatus, SyncStatus.offline);
      expect(server.calls.map((record) => record.asset), ['USD', 'CNY', 'EUR']);
      final unknown = server.calls.last;
      await client.close();
      client = openMobile();

      final resumed = (await client.restore())!;
      expect(resumed.syncStatus, SyncStatus.pending);
      expect(server.calls.map((record) => record.asset), [
        'USD',
        'CNY',
        'EUR',
        'EUR',
      ]);
      expect(server.calls.last, unknown);
      expect(resumed.ledgers.single.accounts.single.assetIds, ['EUR']);
      expect(resumed.assets.map((asset) => asset.id), ['USD', 'CNY', 'EUR']);
      final entries = {
        for (final entry in resumed.entries) entry.draft.assetId: entry,
      };
      for (final asset in ['USD', 'CNY']) {
        expect(entries[asset]?.lifecycle, EntryLifecycle.conflict);
        expect(entries[asset]?.canEdit, isTrue);
        expect(entries[asset]?.draft.amount, '12.34');
      }
      expect(entries['EUR']?.lifecycle, EntryLifecycle.posted);
      expect(entries['EUR']?.draft.amount, '12.34');
      expect(server.applied, hasLength(1));
    },
  );

  for (final denied in [401, 403]) {
    test(
      'explicit $denied hides the workspace and replays its decision after re-login',
      () async {
        await _seedConflict(directory, server.url);
        final initial = await client.signIn(
          server.url,
          'fictional-owner',
          'fictional',
        );
        expect(initial.conflicts.single.id, _intent);

        server.resolutionStatus = 503;
        final offline = await client.resolveConflict(
          _intent,
          ConflictChoice.keepCurrent,
        );
        expect(offline.syncStatus, SyncStatus.offline);
        expect(offline.conflicts.single.id, _intent);

        server.resolutionStatus = denied;
        final locked = await client.resolveConflict(
          _intent,
          ConflictChoice.keepCurrent,
        );
        expect(locked.syncStatus, SyncStatus.signInRequired);
        expect(locked.ownerLabel, isEmpty);
        expect(locked.ledgers, isEmpty);
        expect(locked.entries, isEmpty);
        expect(locked.conflicts, isEmpty);
        expect(await client.restore(), isNull);

        server.resolutionStatus = 200;
        final restored = await client.signIn(
          server.url,
          'fictional-owner',
          'fictional',
        );
        expect(restored.syncStatus, SyncStatus.synced);
        expect(restored.conflicts, isEmpty);
        expect(server.decisions, hasLength(3));
        expect(server.decisions.toSet(), hasLength(1));
        expect(jsonDecode(server.decisions.first), {
          'expected_current_version': 2,
        });
      },
    );
  }
}

Future<void> _seedConflict(Directory directory, Uri url) async {
  final store = await OfflineStore.open(
    databaseFactoryFfi,
    '${directory.path}/workspace.sqlite',
    OfflineScope(url.origin, _owner),
  );
  try {
    await store.enqueue(
      FrozenIntent(
        id: _intent,
        ledgerId: _ledger,
        commandKey: _intent,
        bodyJson: jsonEncode({
          'ledger_id': _ledger,
          'command': {
            'kind': 'cancel',
            'operation_id': _operation,
            'payload': {'expected_version': 1},
          },
        }),
      ),
    );
    await store.markAttempted(_intent);
    await store.settle(
      _intent,
      jsonEncode({
        'outcome': 'conflict',
        'operation_id': _operation,
        'basis': {'version': 1, 'journals': []},
        'competing': {'version': 2, 'journals': []},
      }),
    );
  } finally {
    await store.close();
  }
}

MobileDraft _draft(String asset) => MobileDraft(
  ledgerId: _ledger,
  kind: EntryKind.expense,
  accountId: _bank,
  categoryId: _category,
  assetId: asset,
  amount: '12.34',
  date: '2026-11-25',
  description: 'Fictional receipt in $asset',
);

final class _DraftServer {
  _DraftServer(this.http);

  static Future<_DraftServer> open() async {
    final http = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final fixture = _DraftServer(http);
    http.listen(fixture.handle);
    return fixture;
  }

  final HttpServer http;
  final calls = <({String asset, String body, String path})>[];
  final applied = <String, Object>{};
  bool disabled = false;
  bool loseNextReceipt = false;
  final decisions = <String>[];
  int resolutionStatus = 200;

  Uri get url => Uri.parse('http://127.0.0.1:${http.port}');

  Future<void> handle(HttpRequest request) async {
    final path = request.uri.path.substring('/api/v1'.length);
    final response = request.response;
    response.headers.contentType = ContentType.json;
    Object? result;
    if (path == '/auth/login' || path == '/auth/session') {
      if (path == '/auth/login') {
        await request.drain<void>();
        response.cookies.add(Cookie('coinpup_session', 'synthetic'));
      }
      result = {'user': _user, 'csrf_token': 'synthetic-csrf'};
    } else if (path == '/changes/start' || path.endsWith('/checkpoint')) {
      result = _anchor;
    } else if (path == '/assets') {
      result = [
        for (final id in ['USD', 'CNY', 'EUR'])
          {
            'asset_id': id,
            'code': id,
            'scale': 2,
            'enabled': !disabled || id != 'USD',
          },
      ];
    } else if (path == '/entities') {
      result = [
        {
          'name': 'Fictional mobile test company',
          'archived': false,
          'ledger': {'id': _ledger, 'base_asset_id': 'USD'},
        },
      ];
    } else if (path == '/ledgers/$_ledger/accounts') {
      result = [
        {
          'id': _bank,
          'name': 'Fictional bank',
          'archived': false,
          'asset_ids': ['USD', if (!disabled) 'CNY', 'EUR'],
        },
      ];
    } else if (path == '/ledgers/$_ledger/categories') {
      result = [
        {
          'id': _category,
          'name': 'Fictional office',
          'archived': false,
          'kind': 'expense',
        },
      ];
    } else if (path == '/quotes/manual') {
      result = {'items': []};
    } else if (path.endsWith('/resolution')) {
      final raw = await utf8.decoder.bind(request).join();
      decisions.add(raw);
      response.statusCode = resolutionStatus;
      result = switch (resolutionStatus) {
        401 => _error('authentication_required'),
        403 => _error('csrf_failed'),
        200 => {
          'intent_id': _intent,
          'device_id': path.split('/')[3],
          'accepted_version': jsonDecode(raw)['expected_current_version'],
        },
        _ => _error('service_unavailable'),
      };
    } else if (path.contains('/financial-pushes/')) {
      final raw = await utf8.decoder.bind(request).join();
      final wire = jsonDecode(raw) as Map<String, dynamic>;
      final payload = wire['command']['payload'] as Map<String, dynamic>;
      final asset = payload['asset_id'] as String;
      calls.add((asset: asset, body: raw, path: path));
      if (disabled && asset != 'EUR') {
        response.statusCode = 409;
        result = _error(
          asset == 'USD' ? 'asset_disabled' : 'account_asset_disabled',
        );
      } else {
        result = applied.putIfAbsent(
          path,
          () => {
            'intent_id': path.split('/').last,
            'device_id': path.split('/')[3],
            'ledger_id': _ledger,
            'operation_id': payload['id'],
            'outcome': 'applied',
            'receipt': {
              ...payload,
              'ledger_id': _ledger,
              'kind': 'expense',
              'version': 1,
            },
          },
        );
        if (loseNextReceipt) {
          loseNextReceipt = false;
          response.statusCode = 503;
          result = _error('service_unavailable');
        }
      }
    } else if (path.startsWith('/sync/devices/') && request.method == 'PUT') {
      await request.drain<void>();
      result = {'state': 'registered'};
    } else {
      response.statusCode = 404;
      result = _error('not_found');
    }
    response.write(jsonEncode(result));
    await response.close();
  }

  static Object _error(String code) => {
    'detail': {'code': code},
  };
}
