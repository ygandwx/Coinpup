import 'dart:convert';
import 'dart:io';

import 'package:coinpup_mobile/mobile_camera.dart';
import 'package:coinpup_mobile/mobile_runtime.dart';
import 'package:coinpup_mobile/mobile_workspace.dart';
import 'package:coinpup_mobile/offline_store.dart';
import 'package:coinpup_mobile/ui/mobile_adapter.dart';
import 'package:crypto/crypto.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

const _sessionKey = 'coinpup.active-session.v1';
const _cameraKey = 'coinpup.camera-request.v1';
const _operation = 'fictional-entry';
const _owner = 'fictional-owner';
const _ledger = 'fictional-ledger';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  sqfliteFfiInit();
  late Directory root;
  late HttpServer server;
  late int authorizationStatus;
  late OfflineScope scope;
  late File photo;
  const vault = FlutterSecureStorage();

  Future<T> inspect<T>(
    Future<T> Function(OfflineStore) work, [
    OfflineScope? override,
  ]) async {
    final store = await OfflineStore.open(
      databaseFactoryFfi,
      '${root.path}/workspace.sqlite',
      override ?? scope,
    );
    try {
      return await work(store);
    } finally {
      await store.close();
    }
  }

  CameraRequest pending({
    String? authority,
    String owner = _owner,
    String ledger = _ledger,
    String entry = _operation,
    int version = 0,
  }) => CameraRequest(
    'b83fb8d1-6c2b-4cc5-a0b2-8050cd8d1723',
    OfflineScope(authority ?? scope.authority, owner),
    ledger,
    entry,
    version,
  );

  CoinpupMobile app([CaptureReceipt? recover]) {
    final app = CoinpupMobile(
      database: databaseFactoryFfi,
      directory: () async => root,
      vault: vault,
      retrieveCameraReceipt: recover,
    );
    addTearDown(app.close);
    return app;
  }

  setUp(() async {
    final previous = HttpOverrides.current;
    HttpOverrides.global = null;
    addTearDown(() => HttpOverrides.global = previous);
    root = await Directory.systemTemp.createTemp('coinpup-camera-recovery-');
    addTearDown(() => root.delete(recursive: true));
    server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    addTearDown(() => server.close(force: true));
    authorizationStatus = HttpStatus.serviceUnavailable;
    server.listen((request) async {
      request.response.statusCode = authorizationStatus;
      await request.response.close();
    });
    scope = OfflineScope('http://127.0.0.1:${server.port}', _owner);
    photo = await File('${root.path}/fictional-receipt.jpg')
        .writeAsBytes([255, 216, 255, 217]);
    FlutterSecureStorage.setMockInitialValues({
      _sessionKey: jsonEncode({
        'server': scope.authority,
        'user': {'id': _owner, 'username': 'Fictional owner'},
        'cookie': 'session=fictional',
        'csrf': 'fictional',
      }),
    });
    await inspect((store) async {
      await store.put(
        'sync',
        'catalog',
        jsonEncode({
          'ledgers': [
            {
              'id': _ledger,
              'name': 'Fictional original ledger',
              'archived': false,
              'accounts': [],
              'categories': [],
            },
            {
              'id': 'other-ledger',
              'name': 'Fictional second ledger',
              'archived': false,
              'accounts': [],
              'categories': [],
            },
          ],
        }),
      );
      for (final ledger in [_ledger, 'other-ledger']) {
        final draft = MobileDraft(
          ledgerId: ledger,
          kind: EntryKind.expense,
          accountId: 'fictional-bank',
          categoryId: 'fictional-category',
          assetId: 'USD',
          amount: '1.00',
          date: '2026-11-25',
          description: 'Fictional photo draft',
        );
        await store.put(
          'draft',
          localEntryKey(ledger, _operation),
          jsonEncode({
            'operation_id': _operation,
            'version': 0,
            'draft': draftJson(draft),
          }),
        );
      }
    });
  });

  MobileEntry original(MobileSnapshot view) =>
      view.entries.singleWhere((entry) => entry.ledgerId == _ledger);

  Iterable<AttachmentState> states(MobileSnapshot view) =>
      original(view).attachments.map((photo) => photo.state);

  test(
    'persist before launch and reuse one photo after process loss',
    () async {
      final first = app();
      final draft = original((await first.restore())!);
      late CameraRequest beforeLaunch;
      await expectLater(
        first.capturePhoto(draft, () async {
          beforeLaunch = CameraRequest.decode(
            (await vault.read(key: _cameraKey))!,
          );
          expect(beforeLaunch.owns(scope), isTrue);
          expect(beforeLaunch.ledger, _ledger);
          expect(beforeLaunch.entry, _operation);
          expect(beforeLaunch.version, 0);
          expect(beforeLaunch.path, isNull);
          throw StateError('Fictional process loss before normal result');
        }),
        throwsStateError,
      );
      await first.close();

      final restarted = app(() async => photo.path);
      final reopened = (await restarted.restore())!;
      expect(reopened.syncStatus, SyncStatus.offline);
      expect(restarted.cameraRecoveryFailed, isFalse);
      expect(states(reopened), [AttachmentState.queued]);
      expect(
        reopened.entries
            .singleWhere((row) => row.ledgerId != _ledger)
            .attachments,
        isEmpty,
      );
      expect(await vault.read(key: _cameraKey), isNull);
      await restarted.close();

      // Simulate another process crash just before deleting the pending marker.
      await vault.write(key: _cameraKey, value: beforeLaunch.encode());
      await photo.delete();
      final replay = app();
      expect(states((await replay.restore())!), [AttachmentState.queued]);
      expect(replay.cameraRecoveryFailed, isFalse);
      expect(await vault.read(key: _cameraKey), isNull);
      await replay.close();

      final duplicate = app(() async => photo.path);
      expect(states((await duplicate.restore())!), [AttachmentState.queued]);
      expect(duplicate.cameraRecoveryFailed, isTrue);
      await duplicate.close();
      final jobs = await inspect((store) => store.records('photo'));
      expect(jobs.keys, [beforeLaunch.id]);
      final private = Directory('${root.path}/photos');
      expect(
        private
            .listSync(recursive: true)
            .whereType<File>()
            .map((file) => file.uri.pathSegments.last),
        [beforeLaunch.id],
      );
    },
  );

  for (final mismatch in ['owner', 'server', 'entry', 'ledger', 'version']) {
    test('refuses a result with a different $mismatch', () async {
      final request = pending(
        owner: mismatch == 'owner' ? 'another-owner' : _owner,
        authority: mismatch == 'server' ? 'https://fictional.invalid' : null,
        ledger: mismatch == 'ledger' ? 'missing-ledger' : _ledger,
        entry: mismatch == 'entry' ? 'deleted-entry' : _operation,
        version: mismatch == 'version' ? 1 : 0,
      );
      await vault.write(key: _cameraKey, value: request.encode());
      final client = app(() async => photo.path);
      final state = (await client.restore())!;
      expect(client.cameraRecoveryFailed, isTrue);
      expect(state.entries.every((entry) => entry.attachments.isEmpty), isTrue);
      expect(await vault.read(key: _cameraKey), isNull);
    });
  }

  test(
    'authorization denial drains the result and never opens the queue',
    () async {
      await vault.write(key: _cameraKey, value: pending().encode());
      authorizationStatus = HttpStatus.unauthorized;
      var retrieved = 0;
      final client = app(() async {
        retrieved++;
        return photo.path;
      });
      final state = (await client.restore())!;
      expect(state.syncStatus, SyncStatus.signInRequired);
      expect(state.entries, isEmpty);
      expect(client.cameraRecoveryFailed, isTrue);
      expect(retrieved, 1);
      expect(await vault.read(key: _cameraKey), isNull);
      expect(await inspect((store) => store.records('photo')), isEmpty);
    },
  );

  test(
    'unsupported platform and orphan result never add an attachment',
    () async {
      await vault.write(key: _cameraKey, value: pending().encode());
      final client = app(
        () async => throw UnsupportedError('Fictional platform'),
      );
      expect(original((await client.restore())!).attachments, isEmpty);
      expect(client.cameraRecoveryFailed, isTrue);
      await client.close();
      await vault.delete(key: _sessionKey);
      await vault.delete(key: _cameraKey);
      final noSession = app(() async => photo.path);
      expect(await noSession.restore(), isNull);
      expect(noSession.cameraRecoveryFailed, isTrue);
      expect(await inspect((store) => store.records('photo')), isEmpty);
    },
  );

  test('cancel removes the request without creating a queue entry', () async {
    final client = app();
    final draft = original((await client.restore())!);
    expect(
      original(await client.capturePhoto(draft, () async => null)).attachments,
      isEmpty,
    );
    expect(await vault.read(key: _cameraKey), isNull);
  });

  test(
    'resumes a previously returned path before the private file copy',
    () async {
      final request = pending()..path = photo.path;
      await vault.write(key: _cameraKey, value: request.encode());
      final restarted = app(() async => null);
      expect(states((await restarted.restore())!), [AttachmentState.queued]);
      expect(restarted.cameraRecoveryFailed, isFalse);
      expect(await vault.read(key: _cameraKey), isNull);
      await restarted.close();
      expect((await inspect((store) => store.records('photo'))).keys, [
        request.id,
      ]);
    },
  );

  test(
    'recovers a completed private copy after the camera cache is gone',
    () async {
      final request = pending()..path = photo.path;
      await vault.write(key: _cameraKey, value: request.encode());
      final scopedDirectory = sha256.convert(
        utf8.encode(jsonEncode(scope.keys)),
      );
      final private = File(
        '${root.path}/photos/$scopedDirectory/${request.id}',
      );
      await private.parent.create(recursive: true);
      await photo.copy(private.path);
      await photo.delete();
      final restarted = app();
      expect(states((await restarted.restore())!), [AttachmentState.queued]);
      expect(restarted.cameraRecoveryFailed, isFalse);
      await restarted.close();
      final jobs = await inspect((store) => store.records('photo'));
      expect(jobs.keys, [request.id]);
      expect(
        jsonDecode(jobs[request.id]!)['filename'],
        'fictional-receipt.jpg',
      );
      expect(await private.exists(), isTrue);
    },
  );

  test('normal camera completion uses the same pre-launch record', () async {
    final client = app();
    final draft = original((await client.restore())!);
    late String requestId;
    final captured = await client.capturePhoto(draft, () async {
      requestId = CameraRequest.decode((await vault.read(key: _cameraKey))!).id;
      return photo.path;
    });
    expect(states(captured), [AttachmentState.queued]);
    expect(await vault.read(key: _cameraKey), isNull);
    await client.close();
    expect((await inspect((store) => store.records('photo'))).keys, [
      requestId,
    ]);
  });
}
