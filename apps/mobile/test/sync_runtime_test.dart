import 'dart:convert';
import 'dart:io';

import 'package:coinpup_mobile/mobile_runtime.dart';
import 'package:coinpup_mobile/ui/mobile_adapter.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final environment = Platform.environment;
  final target = environment['COINPUP_MOBILE_TEST_URL'];
  test(
    'real PG17 protocol survives lost replies, two devices and a restart',
    () async {
      final endpoint = Uri.parse(target!);
      expect(endpoint.isScheme('http'), isTrue);
      expect(endpoint.host, '127.0.0.1');
      final previous = HttpOverrides.current;
      HttpOverrides.global = null;
      addTearDown(() => HttpOverrides.global = previous);
      final root = await Directory.systemTemp.createTemp(
        'coinpup-mobile-protocol-',
      );
      addTearDown(() => root.delete(recursive: true));
      final proxy = await _LoopbackFaults.open(endpoint);
      addTearDown(proxy.close);
      FlutterSecureStorage.setMockInitialValues({});
      sqfliteFfiInit();
      CoinpupMobile app(String name) {
        final app = CoinpupMobile(
          database: databaseFactoryFfi,
          directory: () async => Directory('${root.path}/$name'),
          vault: const FlutterSecureStorage(),
        );
        addTearDown(app.close);
        return app;
      }

      Future<MobileSnapshot> signIn(CoinpupMobile client) => client.signIn(
        proxy.address,
        environment['COINPUP_MOBILE_TEST_USERNAME']!,
        environment['COINPUP_MOBILE_TEST_PASSWORD']!,
      );
      var first = app('one');
      final initial = await signIn(first);
      expect(initial.syncStatus, SyncStatus.synced);
      expect(initial.ledgers.length, greaterThanOrEqualTo(2));
      final ledger = initial.ledgers.firstWhere(
        (item) =>
            item.accounts.any((a) => a.assetIds.isNotEmpty) &&
            item.categories.any((c) => c.kind == EntryKind.expense),
      );
      final account = ledger.accounts.firstWhere((a) => a.assetIds.isNotEmpty);
      final asset = initial.assets.firstWhere(
        (a) => account.assetIds.contains(a.id) && a.scale >= 2,
      );
      final category = ledger.categories.firstWhere(
        (c) => c.kind == EntryKind.expense,
      );
      final date = DateTime.now().toUtc().toIso8601String().substring(0, 10);
      final label =
          'Fictional mobile acceptance ${DateTime.now().microsecondsSinceEpoch}';
      MobileDraft draft(String description, String amount) => MobileDraft(
        ledgerId: ledger.id,
        kind: EntryKind.expense,
        accountId: account.id,
        categoryId: category.id,
        assetId: asset.id,
        amount: amount,
        date: date,
        description: '$label $description',
      );
      MobileEntry entry(MobileSnapshot snapshot, String id) =>
          snapshot.entries.singleWhere(
            (item) => item.id == id && item.draft.ledgerId == ledger.id,
          );

      proxy.offline = true;
      final extra = (await first.save(draft('disposable', '10.00'))).entries
          .singleWhere((item) => item.draft.description == '$label disposable');
      expect(
        (await first.delete(extra)).entries.any((item) => item.id == extra.id),
        isFalse,
      );
      final queued = (await first.save(draft('offline', '12.30'))).entries
          .singleWhere((item) => item.draft.description == '$label offline');
      final staged = await first.save(
        draft('edited offline', '12.31'),
        previous: queued,
      );
      expect(entry(staged, queued.id).draft.amount, '12.31');
      await first.close();
      first = app('one');
      final reopened = (await first.restore())!;
      expect(reopened.syncStatus, SyncStatus.offline);
      expect(entry(reopened, queued.id).draft.amount, '12.31');
      proxy.offline = false;
      proxy.dropNextPush = true;
      expect((await first.synchronize()).syncStatus, SyncStatus.offline);
      expect(proxy.droppedPush, isNotNull);
      proxy.dropNextAck = true;
      expect((await first.synchronize()).syncStatus, SyncStatus.offline);
      expect(proxy.droppedAckBody, isNotNull);
      final posted = await first.synchronize();
      expect(posted.syncStatus, SyncStatus.synced);
      expect(proxy.requestBodies[proxy.droppedPush], hasLength(2));
      expect(proxy.requestBodies[proxy.droppedPush]!.toSet(), hasLength(1));
      expect(
        proxy.requestBodies[proxy.droppedAckPath]!.where(
          (body) => body == proxy.droppedAckBody,
        ),
        hasLength(2),
      );
      expect(entry(posted, queued.id).version, 1);

      final second = app('two');
      final secondView = await signIn(second);
      expect(entry(secondView, queued.id).draft.amount, '12.31');
      await first.save(
        draft('first variant', '13.00'),
        previous: entry(posted, queued.id),
      );
      await second.save(
        draft('second variant', '14.00'),
        previous: entry(secondView, queued.id),
      );
      final secondPosted = await second.synchronize();
      expect(entry(secondPosted, queued.id).version, 2);
      final conflictView = await first.synchronize();
      final conflict = conflictView.conflicts.single;
      expect(conflict.submitted?.amount, '13.00');
      expect(conflict.current?.amount, '14.00');
      expect(conflict.submittedVersion, 1);
      expect(conflict.currentVersion, 2);
      await second.save(
        draft('further server edit', '14.75'),
        previous: entry(secondPosted, queued.id),
      );
      final latestOnSecond = await second.synchronize();
      final firstUpdated = await first.synchronize();
      expect(firstUpdated.conflicts.single.currentVersion, 2);
      expect(firstUpdated.conflicts.single.current?.amount, '14.00');
      expect(firstUpdated.conflicts.single.latestVersion, 3);
      expect(firstUpdated.conflicts.single.latest?.amount, '14.75');
      proxy.dropNextDecision = true;
      final offlineDecision = await first.resolveConflict(
        conflict.id,
        ConflictChoice.keepCurrent,
      );
      expect(offlineDecision.syncStatus, SyncStatus.offline);
      expect(offlineDecision.conflicts.single.id, conflict.id);
      final accepted = await first.synchronize();
      expect(proxy.requestBodies[proxy.droppedDecision], hasLength(2));
      expect(proxy.requestBodies[proxy.droppedDecision]!.toSet(), hasLength(1));
      expect(accepted.conflicts, isEmpty);
      expect(entry(accepted, queued.id).draft.amount, '14.75');

      await second.save(
        draft('old device', '15.00'),
        previous: entry(latestOnSecond, queued.id),
      );
      await first.delete(entry(accepted, queued.id));
      final cancelled = await first.synchronize();
      expect(cancelled.entries.any((item) => item.id == queued.id), isFalse);
      final cannotRevive = await second.synchronize();
      expect(cannotRevive.entries.any((item) => item.id == queued.id), isFalse);
      final deleteConflict = cannotRevive.conflicts.single;
      expect(deleteConflict.serverDeleted, isTrue);
      expect(deleteConflict.canKeepLocal, isFalse);
      expect(
        () => second.resolveConflict(
          deleteConflict.id,
          ConflictChoice.submitLocal,
        ),
        throwsA(isA<MobileFailure>()),
      );
      await second.save(draft('authorization preserved', '20.00'));
      proxy.denySession = true;
      final denied = await second.synchronize();
      expect(denied.syncStatus, SyncStatus.signInRequired);
      expect(denied.entries, isEmpty);
      expect(denied.ledgers, isEmpty);
      expect(await second.restore(), isNull);
      proxy.denySession = false;
      final renewed = await signIn(second);
      expect(
        renewed.entries.any(
          (item) => item.draft.description == '$label authorization preserved',
        ),
        isTrue,
      );
    },
    skip: target == null
        ? 'Use the isolated synthetic Coinpup HTTP fixture'
        : false,
  );
}

final class _LoopbackFaults {
  _LoopbackFaults(this._server, this._target) {
    _server.listen(_handle);
  }

  static Future<_LoopbackFaults> open(Uri target) async =>
      _LoopbackFaults(await HttpServer.bind('127.0.0.1', 0), target);

  final HttpServer _server;
  final Uri _target;
  final _client = HttpClient();
  final requestBodies = <String, List<String>>{};
  bool offline = false;
  bool denySession = false;
  bool dropNextPush = false;
  bool dropNextAck = false;
  bool dropNextDecision = false;
  String? droppedPush;
  String? droppedAckBody;
  String? droppedAckPath;
  String? droppedDecision;

  Uri get address => Uri.parse('http://127.0.0.1:${_server.port}');

  Future<void> close() async {
    _client.close(force: true);
    await _server.close(force: true);
  }

  Future<void> _handle(HttpRequest incoming) async {
    final response = incoming.response;
    if (offline) {
      response.statusCode = 503;
      await response.close();
      return;
    }
    final path = incoming.uri.path;
    if (denySession && path == '/api/v1/auth/session') {
      response.statusCode = 401;
      await response.close();
      return;
    }
    final request = await _client.openUrl(
      incoming.method,
      _target.resolveUri(incoming.uri),
    );
    request.followRedirects = false;
    for (final key in ['cookie', 'x-csrf-token', 'content-type']) {
      final value = incoming.headers.value(key);
      if (value != null) request.headers.set(key, value);
    }
    if (incoming.headers.value('origin') != null) {
      request.headers.set('origin', _target.origin);
    }
    final bytes = await incoming.fold<List<int>>(
      [],
      (whole, part) => whole..addAll(part),
    );
    final deciding = incoming.method == 'PUT' && path.endsWith('/resolution');
    final pushing =
        incoming.method == 'PUT' &&
        path.contains('/financial-pushes/') &&
        !deciding;
    final acknowledging =
        incoming.method == 'POST' && path.endsWith('/acknowledgements');
    if (pushing || acknowledging || deciding) {
      requestBodies.putIfAbsent(path, () => []).add(base64Encode(bytes));
    }
    request.contentLength = bytes.length;
    request.add(bytes);
    final upstream = await request.close();
    final body = await upstream.fold<List<int>>(
      [],
      (whole, part) => whole..addAll(part),
    );
    if (upstream.statusCode == 200 &&
        ((pushing && dropNextPush) ||
            (acknowledging && dropNextAck) ||
            (deciding && dropNextDecision))) {
      if (pushing) {
        droppedPush = path;
        dropNextPush = false;
      } else if (acknowledging) {
        droppedAckBody = base64Encode(bytes);
        droppedAckPath = path;
        dropNextAck = false;
      } else {
        droppedDecision = path;
        dropNextDecision = false;
      }
      (await response.detachSocket(writeHeaders: false)).destroy();
      return;
    }
    response.statusCode = upstream.statusCode;
    for (final key in ['content-type', 'set-cookie']) {
      for (final value in upstream.headers[key] ?? const <String>[]) {
        response.headers.add(key, value);
      }
    }
    response.contentLength = body.length;
    response.add(body);
    await response.close();
  }
}
