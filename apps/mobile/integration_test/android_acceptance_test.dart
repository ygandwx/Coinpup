import 'dart:io';

import 'package:coinpup_mobile/main.dart' as app;
import 'package:coinpup_mobile/mobile_http.dart';
import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart' show listEquals;
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:material_ui/material_ui.dart';
import 'package:path_provider/path_provider.dart';

const _ledgerName = 'Fictional Android acceptance ledger';
const _description = 'Fictional Android offline stationery';
const _amount = '12.31';

void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('Android sign-in, offline posting, sync and native receipt', (
    tester,
  ) async {
    expect(Platform.isAndroid, isTrue);
    final upstream = Uri.parse(
      const String.fromEnvironment('COINPUP_TEST_URL'),
    );
    expect(upstream.origin, 'http://127.0.0.1:8000');
    const username = String.fromEnvironment('COINPUP_TEST_USERNAME');
    const password = String.fromEnvironment('COINPUP_TEST_PASSWORD');
    expect(username.isNotEmpty && password.isNotEmpty, isTrue);

    final api = MobileHttp(upstream);
    addTearDown(api.close);
    final ledger = await _seedLedger(api, username, password);
    final operationsPath = '/ledgers/$ledger/operations';
    final relay = _LoopbackRelay(upstream);
    addTearDown(relay.close);
    await relay.connect();

    binding.platformDispatcher.localeTestValue = const Locale('en');
    addTearDown(binding.platformDispatcher.clearLocaleTestValue);
    tester.testTextInput.register();
    addTearDown(tester.testTextInput.unregister);
    app.main();

    await _waitFor(tester, _key('sign-in'));
    await _enter(tester, 'server', relay.address.toString());
    await _enter(tester, 'username', username);
    await _enter(tester, 'password', password);
    await _tap(tester, _key('sign-in'));
    await _waitFor(tester, find.text('Synced'));
    expect(find.text(_ledgerName), findsAtLeastNWidgets(1));
    debugPrint(
      'Android acceptance: signed into disposable PostgreSQL-backed API',
    );

    await relay.disconnect();
    await _sync(tester, 'Working offline');
    await _tap(tester, find.text('Entries'));
    await _tap(tester, _key('new-entry'));
    await tester.pumpAndSettle();
    await _enter(tester, 'entry-amount', _amount, list: 'entry-form-scroll');
    await _enter(
      tester,
      'entry-description',
      _description,
      list: 'entry-form-scroll',
    );
    await _tap(tester, _key('save-entry'), list: 'entry-form-scroll');
    final amount = find.text('$_amount USD');
    await _waitFor(tester, amount);
    await tester.pumpAndSettle();
    final entry = find.ancestor(of: amount, matching: find.byType(Card));
    final identity =
        (tester.widget<Card>(entry).key! as ValueKey<String>).value;
    expect(identity, startsWith('entry-'));
    final operationId = identity.substring('entry-'.length);
    Finder inEntry(String label) =>
        find.descendant(of: _key(identity), matching: find.text(label));
    Future<void> showEntry(String text) async {
      final target = inEntry(text);
      if (target.evaluate().isEmpty) {
        await _tap(tester, amount, list: 'workspace-scroll');
        await tester.pumpAndSettle();
      }
      expect(target, findsOneWidget);
    }

    await showEntry('Saved on this device');
    expect(inEntry(_description), findsOneWidget);
    expect(await api.list(operationsPath), isEmpty);
    debugPrint(
      'Android acceptance: offline expense persisted locally with no posting',
    );

    await relay.connect();
    await _sync(tester, 'Synced');
    final operation = (await api.list(operationsPath)).single;
    expect(operation['id'], operationId);
    expect(operation['kind'], 'expense');
    expect(operation['version'], 1);
    final posted = readObject(operation['latest_posting']);
    expect(posted['amount'], _amount);
    expect(posted['description'], _description);
    debugPrint(
      'Android acceptance: reconnection posted the exact expense once',
    );

    await _tap(tester, find.text('Entries'));
    await showEntry('Posted');
    debugPrint('Android acceptance: opening the native camera');
    await _tap(tester, inEntry('Photograph receipt'), list: 'workspace-scroll');
    final queued = inEntry('Receipt queued');
    await _waitFor(tester, queued, timeout: const Duration(seconds: 85));
    final receipt = find.ancestor(
      of: queued,
      matching: find.byWidgetPredicate(
        (widget) =>
            widget.key is ValueKey<String> &&
            (widget.key! as ValueKey<String>).value.startsWith('receipt-'),
      ),
    );
    final receiptKey = (tester.widget(receipt).key! as ValueKey<String>).value;
    final uploadId = receiptKey.substring('receipt-'.length);
    final photos = Directory(
      '${(await getApplicationSupportDirectory()).path}/photos',
    );
    final captured = await photos
        .list(recursive: true)
        .where((file) => file is File && file.uri.pathSegments.last == uploadId)
        .cast<File>()
        .single;
    final original = await captured.readAsBytes();
    expect(original.take(3), [255, 216, 255]);
    final linksPath = '$operationsPath/$operationId/files';
    expect(await api.list(linksPath), isEmpty);

    await _sync(tester, 'Synced');
    await _tap(tester, find.text('Entries'));
    await showEntry('Receipt uploaded');
    expect((await api.list(operationsPath)).single['id'], operationId);
    final link = (await api.list(linksPath)).single;
    expect(link['operation_id'], operationId);
    expect(link['archived'], isFalse);
    final upload = readObject(
      await api.request('GET', '/ledgers/$ledger/uploads/$uploadId'),
    );
    expect(upload['state'], 'ready');
    expect(upload['operation_id'], operationId);
    final completion = readObject(upload['response']);
    expect(completion['file_id'], link['file_id']);
    expect(completion['link_id'], link['id']);
    expect(completion['byte_size'], original.length);
    expect(completion['sha256'], sha256.convert(original).toString());
    final client = HttpClient();
    addTearDown(() => client.close(force: true));
    final download = await client.getUrl(
      upstream.replace(
        path: '/api/v1/ledgers/$ledger/files/${link['file_id']}/content',
      ),
    );
    download.headers.set('Cookie', api.sessionCookie!);
    final response = await download.close();
    expect(response.statusCode, HttpStatus.ok);
    final downloaded = await response.expand((chunk) => chunk).toList();
    expect(listEquals(downloaded, original), isTrue);
    debugPrint(
      'Android acceptance: native camera original uploaded, linked and downloaded byte-for-byte',
    );
  }, timeout: const Timeout(Duration(minutes: 5)));
}

Future<String> _seedLedger(
  MobileHttp api,
  String username,
  String password,
) async {
  final login = readObject(
    await api.request(
      'POST',
      '/auth/login',
      json: {'username': username, 'password': password},
    ),
  );
  api.csrf = login['csrf_token'] as String;
  expect(api.sessionCookie, isNotNull);
  final entity = readObject(
    await api.request(
      'POST',
      '/entities',
      json: {
        'kind': 'personal',
        'name': _ledgerName,
        'base_asset_id': 'USD',
        'locale': 'en',
      },
    ),
  );
  final ledger = readObject(entity['ledger'])['id'] as String;
  await api.request(
    'POST',
    '/ledgers/$ledger/accounts',
    json: {
      'name': 'Fictional cash',
      'kind': 'cash',
      'asset_ids': ['USD'],
    },
  );
  await api.request(
    'POST',
    '/ledgers/$ledger/categories',
    json: {'name': 'Fictional stationery', 'kind': 'expense'},
  );
  return ledger;
}

Finder _key(String name) => find.byKey(ValueKey(name));

Future<void> _waitFor(
  WidgetTester tester,
  Finder target, {
  Duration timeout = const Duration(seconds: 30),
}) async {
  final elapsed = Stopwatch()..start();
  while (target.evaluate().isEmpty && elapsed.elapsed < timeout) {
    await tester.pump(const Duration(milliseconds: 100));
  }
  expect(target, findsOneWidget);
}

Future<void> _reveal(WidgetTester tester, Finder target, String? list) async {
  if (list != null && target.evaluate().isEmpty) {
    await tester.scrollUntilVisible(
      target,
      120,
      scrollable: find
          .descendant(of: _key(list), matching: find.byType(Scrollable))
          .first,
    );
  }
  await tester.ensureVisible(target);
  await tester.pump();
}

Future<void> _enter(
  WidgetTester tester,
  String field,
  String text, {
  String? list,
}) async {
  final target = _key(field);
  await _reveal(tester, target, list);
  await tester.enterText(target, text);
}

Future<void> _tap(WidgetTester tester, Finder target, {String? list}) async {
  await _reveal(tester, target, list);
  await tester.tap(target);
  await tester.pump();
}

Future<void> _sync(WidgetTester tester, String expectedStatus) async {
  await _tap(tester, find.text('Sync'));
  await _tap(tester, _key('sync-now'), list: 'workspace-scroll');
  await _waitFor(
    tester,
    find.byWidgetPredicate(
      (widget) =>
          widget is FilledButton &&
          widget.key == const ValueKey('sync-now') &&
          widget.onPressed != null &&
          find.text(expectedStatus).evaluate().length == 1,
    ),
  );
}

/// Closing the listening socket makes the unmodified app lose its connection
/// while the fixture's independent connection to Compose stays available.
final class _LoopbackRelay {
  _LoopbackRelay(this.upstream);

  final Uri upstream;
  final _client = HttpClient();
  HttpServer? _server;
  int _port = 0;

  Uri get address => Uri(scheme: 'http', host: '127.0.0.1', port: _port);

  Future<void> connect() async {
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, _port);
    _server = server;
    _port = server.port;
    server.listen(_forward);
  }

  Future<void> disconnect() async {
    await _server?.close(force: true);
    _server = null;
  }

  Future<void> close() async {
    await disconnect();
    _client.close(force: true);
  }

  Future<void> _forward(HttpRequest incoming) async {
    final request = await _client.openUrl(
      incoming.method,
      upstream.resolveUri(incoming.uri),
    );
    request.followRedirects = false;
    for (final name in [
      'cookie',
      'content-type',
      'x-csrf-token',
      'x-coinpup-device-id',
    ]) {
      final value = incoming.headers.value(name);
      if (value != null) request.headers.set(name, value);
    }
    if (incoming.headers.value('origin') != null) {
      request.headers.set('Origin', upstream.origin);
    }
    final bytes = await incoming.expand((chunk) => chunk).toList();
    request.contentLength = bytes.length;
    request.add(bytes);
    final upstreamResponse = await request.close();
    final response = incoming.response;
    response.statusCode = upstreamResponse.statusCode;
    for (final name in ['content-type', 'set-cookie']) {
      for (final value in upstreamResponse.headers[name] ?? const <String>[]) {
        response.headers.add(name, value);
      }
    }
    await response.addStream(upstreamResponse);
    await response.close();
  }
}
