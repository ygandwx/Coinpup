import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:flutter/foundation.dart';

import 'ui/mobile_adapter.dart';

typedef Json = Map<String, dynamic>;

Json readObject(Object? value) => (value as Map).cast<String, dynamic>();
List<Json> readRows(Object? value) => (value as List).map(readObject).toList();

String newMobileId() {
  final random = Random.secure();
  final bytes = List.generate(16, (_) => random.nextInt(256));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  final hex = bytes
      .map((byte) => byte.toRadixString(16).padLeft(2, '0'))
      .join();
  return '${hex.substring(0, 8)}-${hex.substring(8, 12)}-'
      '${hex.substring(12, 16)}-${hex.substring(16, 20)}-${hex.substring(20)}';
}

Uri validatedMobileServer(Uri uri) {
  final loopback = const {'localhost', '127.0.0.1', '::1'}.contains(uri.host);
  if ((!uri.isScheme('https') &&
          !(kDebugMode && uri.isScheme('http') && loopback)) ||
      uri.host.isEmpty ||
      uri.userInfo.isNotEmpty ||
      uri.hasQuery ||
      uri.hasFragment ||
      (uri.path.isNotEmpty && uri.path != '/')) {
    throw const MobileFailure(MobileErrorCode.serverAddress);
  }
  return Uri.parse(uri.origin);
}

final class MobileHttpError implements Exception {
  const MobileHttpError(this.status, this.code);

  final int status;
  final String? code;
}

final class MobileHttp {
  MobileHttp(this.server) {
    _client.connectionTimeout = const Duration(seconds: 12);
  }

  final Uri server;
  final _client = HttpClient();
  String? sessionCookie;
  String? csrf;

  void close() => _client.close(force: true);

  Future<Object?> request(
    String method,
    String path, {
    Map<String, Object>? query,
    Object? json,
    String? rawJson,
    List<int>? bytes,
    Map<String, String> headers = const {},
  }) async {
    final limit = const Duration(seconds: 25);
    final request = await _client
        .openUrl(
          method,
          server.replace(path: '/api/v1$path', queryParameters: query),
        )
        .timeout(limit);
    request.followRedirects = false;
    request.headers.set('Accept', 'application/json');
    if (sessionCookie != null) request.headers.set('Cookie', sessionCookie!);
    if (method != 'GET') {
      request.headers.set('Origin', server.origin);
      if (csrf != null) request.headers.set('X-CSRF-Token', csrf!);
    }
    headers.forEach(request.headers.set);
    final body =
        bytes ??
        (rawJson != null
            ? utf8.encode(rawJson)
            : (json == null ? null : utf8.encode(jsonEncode(json))));
    if (body != null) {
      request.contentLength = body.length;
      request.headers.contentType = bytes == null
          ? ContentType.json
          : ContentType.binary;
      request.add(body);
    }
    final response = await request.close().timeout(limit);
    final encoded = await response
        .transform(utf8.decoder)
        .join()
        .timeout(limit);
    final data = encoded.isEmpty ? null : _decode(encoded);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      final detail = data is Json ? data['detail'] : null;
      throw MobileHttpError(
        response.statusCode,
        detail is Json
            ? detail['code'] as String?
            : (detail is String ? detail : null),
      );
    }
    for (final cookie in response.cookies) {
      if (cookie.name == 'coinpup_session' ||
          cookie.name == '__Host-coinpup_session') {
        sessionCookie = cookie.value.isEmpty
            ? null
            : '${cookie.name}=${cookie.value}';
      }
    }
    return data;
  }

  static Object? _decode(String encoded) {
    try {
      return jsonDecode(encoded);
    } on FormatException {
      return null;
    }
  }

  Future<List<Json>> list(
    String path, {
    Map<String, String> filters = const {},
    String? items,
  }) async {
    const size = 100;
    final response = <Json>[];
    for (var offset = 0; ; offset += size) {
      final data = await request(
        'GET',
        path,
        query: {...filters, 'limit': '$size', 'offset': '$offset'},
      );
      final page = readRows(items == null ? data : readObject(data)[items]);
      response.addAll(page);
      if (page.length < size) return response;
    }
  }
}
