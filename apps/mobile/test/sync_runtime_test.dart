import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'dart:typed_data';

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
    'real PG17 protocol survives lost replies, two devices and photo resume',
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
      expect(
        initial.quotes.any((q) => q.price != null && q.observedAt != null),
        isTrue,
      );
      if (environment['COINPUP_MOBILE_TEST_EXPECT_AUTOMATIC'] == '1') {
        MobileQuote usd(String asset) => initial.quotes.singleWhere(
          (row) => row.assetId == asset && row.displayAssetId == 'USD',
        );
        expect(usd('BTC').price, '60000.001234567890123456');
        expect(usd('BTC').observedAt, endsWith('00:02:59.654322Z'));
        expect(usd('BTC').cachedAt, endsWith('00:05:00Z'));
        expect(usd('EUR').price, '1.123456789012345678');
        expect(usd('EUR').observedAt, isNull);
        expect(usd('EUR').cachedAt, isNotNull);
        expect(usd('GBP').price, '1.301234567890123456');
        expect(usd('ETH').price, isNull);
      }
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
      final jpg = File('${root.path}/fictional-receipt.png');
      await jpg.writeAsBytes(_syntheticPhoto());
      await first.capturePhoto(entry(staged, queued.id), () async => jpg.path);
      await first.close();
      first = app('one');
      final reopened = (await first.restore())!;
      expect(reopened.syncStatus, SyncStatus.offline);
      expect(entry(reopened, queued.id).draft.amount, '12.31');
      expect(
        entry(reopened, queued.id).attachments.map((photo) => photo.state),
        [AttachmentState.queued],
      );

      proxy.offline = false;
      proxy.dropNextPush = true;
      expect((await first.synchronize()).syncStatus, SyncStatus.offline);
      expect(proxy.droppedPush, isNotNull);
      proxy.dropNextAck = true;
      expect((await first.synchronize()).syncStatus, SyncStatus.offline);
      expect(proxy.droppedAckBody, isNotNull);
      proxy.dropNextChunk = true;
      expect((await first.synchronize()).syncStatus, SyncStatus.offline);
      expect(proxy.droppedChunk, isNotNull);
      var posted = await first.synchronize();
      expect(posted.syncStatus, SyncStatus.synced);
      expect(proxy.requestBodies[proxy.droppedPush], hasLength(2));
      expect(proxy.requestBodies[proxy.droppedPush]!.toSet(), hasLength(1));
      expect(
        proxy.requestBodies[proxy.droppedAckPath]!.where(
          (body) => body == proxy.droppedAckBody,
        ),
        hasLength(2),
      );
      expect(proxy.requestBodies[proxy.droppedChunk], hasLength(1));
      expect(entry(posted, queued.id).attachments.map((photo) => photo.state), [
        AttachmentState.uploaded,
      ]);
      expect(entry(posted, queued.id).version, 1);

      if (environment['COINPUP_MOBILE_TEST_EXPECT_RELEASE'] == '1') {
        final attached = await first.capturePhoto(
          entry(posted, queued.id),
          () async => jpg.path,
        );
        final selected = entry(attached, queued.id).attachments.singleWhere(
          (photo) => photo.state == AttachmentState.queued,
        );
        proxy.dropNextChunk = true;
        final interrupted = await first.synchronize();
        expect(interrupted.syncStatus, SyncStatus.offline);
        proxy.dropNextRelease = true;
        final release = await first.releasePhoto(
          ledger.id,
          queued.id,
          selected.id,
        );
        expect(
          entry(
            release,
            queued.id,
          ).attachments.singleWhere((photo) => photo.id == selected.id).state,
          AttachmentState.releasing,
        );
        expect(proxy.droppedReleaseStatus?['upload_id'], selected.id);
        expect(proxy.droppedReleaseStatus?['next_offset'], 1024 * 1024);
        expect(proxy.droppedReleaseStatus?['state'], 'abandoned');
        await first.close();
        first = app('one');
        posted = (await first.restore())!;
        expect(
          entry(posted, queued.id).attachments.map((photo) => photo.state),
          unorderedEquals([AttachmentState.uploaded, AttachmentState.released]),
        );
        expect(proxy.requestBodies[proxy.droppedRelease], ['', '']);
      }

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
  bool dropNextChunk = false;
  bool dropNextAck = false;
  bool dropNextDecision = false;
  bool dropNextRelease = false;
  String? droppedPush;
  String? droppedChunk;
  String? droppedAckBody;
  String? droppedAckPath;
  String? droppedDecision;
  String? droppedRelease;
  Map<String, dynamic>? droppedReleaseStatus;

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
    for (final key in [
      'cookie',
      'x-csrf-token',
      'content-type',
      'x-coinpup-device-id',
    ]) {
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
    final chunking =
        incoming.method == 'PUT' && path.contains('/resumable/chunks/');
    final releasing =
        incoming.method == 'POST' && path.endsWith('/resumable/abandon');
    if (pushing || chunking || acknowledging || deciding || releasing) {
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
            (chunking && dropNextChunk) ||
            (acknowledging && dropNextAck) ||
            (deciding && dropNextDecision) ||
            (releasing && dropNextRelease))) {
      if (pushing) {
        droppedPush = path;
        dropNextPush = false;
      } else if (chunking) {
        droppedChunk = path;
        dropNextChunk = false;
      } else if (acknowledging) {
        droppedAckBody = base64Encode(bytes);
        droppedAckPath = path;
        dropNextAck = false;
      } else if (deciding) {
        droppedDecision = path;
        dropNextDecision = false;
      } else {
        droppedRelease = path;
        droppedReleaseStatus =
            jsonDecode(utf8.decode(body)) as Map<String, dynamic>;
        dropNextRelease = false;
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

List<int> _syntheticPhoto() {
  const width = 1150;
  const height = 1050;
  final random = Random(1);
  final rows = Uint8List((width + 1) * height);
  for (var y = 0; y < height; y++) {
    for (var x = 1; x <= width; x++) {
      rows[y * (width + 1) + x] = random.nextInt(256);
    }
  }
  List<int> uint32(int value) =>
      (ByteData(4)..setUint32(0, value)).buffer.asUint8List();
  List<int> chunk(String name, List<int> data) {
    final contents = [...ascii.encode(name), ...data];
    var checksum = 0xffffffff;
    for (final value in contents) {
      checksum ^= value;
      for (var bit = 0; bit < 8; bit++) {
        checksum = (checksum >>> 1) ^ ((checksum & 1) == 1 ? 0xedb88320 : 0);
      }
    }
    return [
      ...uint32(data.length),
      ...contents,
      ...uint32(checksum ^ 0xffffffff),
    ];
  }

  return [
    0x89,
    0x50,
    0x4e,
    0x47,
    0x0d,
    0x0a,
    0x1a,
    0x0a,
    ...chunk('IHDR', [...uint32(width), ...uint32(height), 8, 0, 0, 0, 0]),
    ...chunk('IDAT', ZLibEncoder(level: 0).convert(rows)),
    ...chunk('IEND', []),
  ];
}
