import 'dart:convert';
import 'dart:io';

import 'package:coinpup_mobile/mobile_http.dart';
import 'package:coinpup_mobile/mobile_photos.dart';
import 'package:coinpup_mobile/mobile_workspace.dart';
import 'package:coinpup_mobile/offline_store.dart';
import 'package:coinpup_mobile/ui/mobile_adapter.dart';
import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

const _upload = 'fictional-upload';
const _other = 'fictional-other';
const _ledger = 'fictional-ledger';
const _entry = 'fictional-entry';
const _route = '/api/v1/ledgers/$_ledger/uploads/$_upload';
const _abandon = '$_route/resumable/abandon';
const _bytes = [255, 216, 255, 217];
const _draft = MobileDraft(
  ledgerId: _ledger,
  kind: EntryKind.expense,
  accountId: 'fictional-account',
  categoryId: 'fictional-category',
  assetId: 'USD',
  amount: '12.30',
  date: '2026-11-25',
  description: 'Fictional receipt',
);
const _entryView = MobileEntry(
  id: _entry,
  version: 1,
  draft: _draft,
  lifecycle: EntryLifecycle.posted,
);
final _sha = sha256.convert(_bytes).toString();

void main() {
  sqfliteFfiInit();
  late HttpServer server;
  late Directory directory;
  late MobileHttp http;
  late OfflineStore store;
  late MobilePhotos photos;
  late List<
    ({
      String method,
      String path,
      String body,
      String? device,
      String? origin,
      String? csrf,
      String? cookie,
    })
  >
  requests;
  late Future<void> Function(HttpRequest) reply;

  Future<OfflineStore> openStore() => OfflineStore.open(
    databaseFactoryFfi,
    '${directory.path}/workspace.sqlite',
    OfflineScope(http.server.origin, 'fictional-owner'),
  );

  Future<void> seed(String id, String state) async {
    await File('${directory.path}/$id').writeAsBytes(_bytes);
    await store.put(
      'photo',
      id,
      jsonEncode({
        'ledger_id': _ledger,
        'operation_id': _entry,
        'state': state,
        'filename': 'fictional-receipt.jpg',
        'size': _bytes.length,
        'sha256': _sha,
      }),
    );
  }

  Future<String> state([String id = _upload]) async =>
      jsonDecode((await store.record('photo', id))!)['state'] as String;

  Future<void> respond(HttpRequest request, int status, Object body) async {
    request.response.statusCode = status;
    request.response.headers.contentType = ContentType.json;
    request.response.write(jsonEncode(body));
    await request.response.close();
  }

  Json abandoned({String ledger = _ledger}) => {
    'upload_id': _upload,
    'ledger_id': ledger,
    'device_id': 'fictional-previous-device',
    'declared_size': _bytes.length,
    'sha256': _sha,
    'next_offset': 2,
    'max_chunk_bytes': 1024 * 1024,
    'state': 'abandoned',
  };

  setUp(() async {
    final previous = HttpOverrides.current;
    HttpOverrides.global = null;
    addTearDown(() => HttpOverrides.global = previous);
    directory = await Directory.systemTemp.createTemp('coinpup-photo-release-');
    addTearDown(() => directory.delete(recursive: true));
    server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    addTearDown(() => server.close(force: true));
    http = MobileHttp(Uri.parse('http://127.0.0.1:${server.port}'))
      ..sessionCookie = 'coinpup_session=fictional'
      ..csrf = 'fictional-csrf';
    addTearDown(http.close);
    store = await openStore();
    addTearDown(() => store.close());
    photos = MobilePhotos(http, store, 'fictional-device', directory);
    requests = [];
    reply = (request) => respond(request, 200, abandoned());
    server.listen((request) async {
      final body = await request.expand((chunk) => chunk).toList();
      requests.add((
        method: request.method,
        path: request.uri.path,
        body:
            request.headers.contentType?.mimeType == 'application/octet-stream'
            ? base64Encode(body)
            : utf8.decode(body),
        device: request.headers.value('x-coinpup-device-id'),
        origin: request.headers.value('origin'),
        csrf: request.headers.value('x-csrf-token'),
        cookie: request.headers.value('cookie'),
      ));
      await reply(request);
    });
  });

  for (final race in [false, true]) {
    test(
      'canceled history keeps the upload scope after abandonment (race: $race)',
      () async {
        await seed(_upload, 'uploading');
        await seed(_other, 'queued');
        await store.commitWindow(
          after: '0',
          delivered: '1',
          witness: '{}',
          changes: [
            const CachedOperation(
              ledgerId: _ledger,
              id: _entry,
              version: 2,
              json: '{}',
              removed: true,
            ),
          ],
        );
        final prior = await MobileWorkspace(store)
            .snapshot('Fictional owner', SyncStatus.ready);
        expect(prior.entries, isEmpty);
        expect(prior.detachedReceipts.map((row) => row.operationId), [
          _entry,
          _entry,
        ]);
        expect(
          prior.detachedReceipts.every((row) => row.attachment.canRelease),
          isTrue,
        );

        final reservePath = '/api/v1/ledgers/$_ledger/uploads';
        final newer = '$reservePath/$_other/resumable';
        reply = (request) async {
          final path = request.uri.path;
          final original = path.startsWith(_route);
          final id = original ? _upload : _other;
          final content = Map<String, Object?>.of(abandoned())
            ..addAll({'upload_id': id, 'device_id': 'fictional-device'});
          if (path == reservePath) {
            expect(jsonDecode(requests.last.body)['operation_id'], _entry);
            await respond(request, 201, {});
          } else if (path == '$_route/resumable/chunks/0') {
            expect(race, isTrue);
            await respond(request, 409, {
              'detail': {'code': 'upload_abandoned'},
            });
          } else if (path.endsWith('/chunks/0')) {
            expect(requests.last.body, base64Encode(_bytes));
            await respond(request, 200, {
              ...content,
              'state': 'pending',
              'next_offset': _bytes.length,
            });
          } else if (path == '$newer/complete') {
            await respond(request, 200, {
              'upload_id': _other,
              'byte_size': _bytes.length,
              'sha256': _sha,
              'link_id': 'fictional-canceled-operation-link',
            });
          } else if (path.endsWith('/resumable')) {
            await respond(
              request,
              200,
              original && !race
                  ? content
                  : {...content, 'state': 'pending', 'next_offset': 0},
            );
          } else {
            fail('Unexpected test server route: $path');
          }
        };
        await photos.send();
        expect(await state(_upload), 'released');
        expect(await state(_other), 'uploaded');
        expect(await File('${directory.path}/$_upload').readAsBytes(), _bytes);
        expect(await File('${directory.path}/$_other').readAsBytes(), _bytes);
        expect(await store.operationStamp(_ledger, _entry), (
          version: 2,
          removed: true,
        ));
        final after = await MobileWorkspace(store)
            .snapshot('Fictional owner', SyncStatus.ready);
        expect(after.entries, isEmpty);
        expect(after.pendingCount, 0);
        expect(after.detachedReceipts.map((row) => row.attachment.state), [
          AttachmentState.released,
          AttachmentState.uploaded,
        ]);
        expect(requests.map((item) => item.path), [
          reservePath,
          '$_route/resumable',
          if (race) '$_route/resumable/chunks/0',
          reservePath,
          newer,
          '$newer/chunks/0',
          '$newer/complete',
        ]);
        final sendCount = requests.length;
        await photos.send();
        expect(requests, hasLength(sendCount));
      },
    );
  }

  test(
    'one release survives a lost reply and a new SQLite connection',
    () async {
      await seed(_upload, 'uploading');
      await seed(_other, 'failed');
      reply = (request) async {
        if (requests.length == 1) {
          expect(await state(), 'releasing');
          final connection = await request.response.detachSocket();
          connection.destroy();
        } else {
          await respond(request, 200, abandoned());
        }
      };
      await photos.release(_ledger, _entry, _upload);
      expect(await state(), 'releasing');
      expect(await state(_other), 'failed');
      await store.close();
      store = await openStore();
      photos = MobilePhotos(http, store, 'fictional-device', directory);
      await photos.send();
      await photos.send();
      await photos.release(_ledger, _entry, _upload);
      expect(await state(), 'released');
      expect(await state(_other), 'failed');
      expect(requests, hasLength(2));
      expect(requests[0], requests[1]);
      expect(requests.first, (
        method: 'POST',
        path: _abandon,
        body: '',
        device: null,
        origin: http.server.origin,
        csrf: 'fictional-csrf',
        cookie: 'coinpup_session=fictional',
      ));
      expect(await File('${directory.path}/$_upload').readAsBytes(), _bytes);
      expect(await File('${directory.path}/$_other').readAsBytes(), _bytes);
    },
  );

  test(
    'never-attempted photos and definitive missing reservations stay local',
    () async {
      await seed(_upload, 'failed');
      await seed(_other, 'queued');
      reply = (request) => respond(request, 404, {
        'detail': {'code': 'not_found'},
      });
      await photos.release(_ledger, _entry, _other);
      expect(requests, isEmpty);
      await photos.release(_ledger, _entry, _upload);
      expect(requests.single.path, _abandon);
      expect(await state(), 'released');
      expect(await state(_other), 'released');
      await photos.discardDraft(_entryView);
      expect(await File('${directory.path}/$_upload').readAsBytes(), _bytes);
      expect(await File('${directory.path}/$_other').readAsBytes(), _bytes);
    },
  );

  test(
    'sealed receipt wins a race with release and cannot be released again',
    () async {
      await seed(_upload, 'uploading');
      reply = (request) => request.method == 'POST'
          ? respond(request, 409, {
              'detail': {'code': 'upload_complete'},
            })
          : respond(request, 200, {
              'state': 'ready',
              'id': _upload,
              'ledger_id': _ledger,
              'operation_id': _entry,
              'declared_size': _bytes.length,
              'response': {
                'upload_id': _upload,
                'byte_size': _bytes.length,
                'sha256': _sha,
                'link_id': 'fictional-sealed-link',
              },
            });
      await photos.release(_ledger, _entry, _upload);
      await photos.release(_ledger, _entry, _upload);
      await photos.send();
      expect(requests.map((item) => item.path), [_abandon, _route]);
      expect(await state(), 'uploaded');
      expect(await File('${directory.path}/$_upload').readAsBytes(), _bytes);
    },
  );

  test(
    'foreign scopes and unverified responses cannot release local originals',
    () async {
      await seed(_upload, 'failed');
      await expectLater(
        photos.release(_ledger, 'fictional-different-entry', _upload),
        throwsA(isA<MobileFailure>()),
      );
      expect(requests, isEmpty);
      expect(await state(), 'failed');
      reply = (request) => respond(request, 404, {'detail': 'Not Found'});
      await photos.release(_ledger, _entry, _upload);
      expect(await state(), 'releasing');
      reply = (request) => respond(request, 200, abandoned(ledger: 'foreign'));
      await photos.release(_ledger, _entry, _upload);
      expect(await state(), 'releasing');
      expect(await File('${directory.path}/$_upload').readAsBytes(), _bytes);
    },
  );
}
