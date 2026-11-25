import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:crypto/crypto.dart';
import 'package:path/path.dart' as paths;

import 'mobile_http.dart';
import 'mobile_sync_feed.dart';
import 'offline_store.dart';
import 'ui/mobile_adapter.dart';

final class MobilePhotos {
  const MobilePhotos(this.http, this.store, this.deviceId, this.directory);

  final MobileHttp http;
  final OfflineStore store;
  final String deviceId;
  final Directory directory;

  Future<bool> queued(String id, String ledger, String entry) async {
    final saved = await store.record('photo', id);
    if (saved == null) return false;
    final photo = readJson(saved);
    if (photo['ledger_id'] != ledger || photo['operation_id'] != entry) {
      throw const FormatException('Photo capture scope differs');
    }
    return true;
  }

  Future<void> queue(String id, MobileEntry entry, String temporaryPath) async {
    if (await queued(id, entry.ledgerId, entry.id)) return;
    final input = File(temporaryPath);
    final destination = File(paths.join(directory.path, id));
    // Only a completed private copy is renamed to this stable request path.
    final copied = await destination.exists();
    final source = copied ? destination : input;
    if (!await source.exists()) {
      throw const MobileFailure(MobileErrorCode.photo);
    }
    final size = await source.length();
    if (size < 1 || size > 50 * 1024 * 1024) {
      throw const MobileFailure(MobileErrorCode.photo);
    }
    await directory.create(recursive: true);
    final staging = File('${destination.path}.part');
    try {
      final ready = copied ? destination : await input.copy(staging.path);
      if (await ready.length() != size) {
        throw const FormatException('Photo size changed');
      }
      final digest = await sha256.bind(ready.openRead()).first;
      if (!copied) await ready.rename(destination.path);
      await store.put(
        'photo',
        id,
        jsonEncode({
          'ledger_id': entry.draft.ledgerId,
          'operation_id': entry.id,
          'state': 'queued',
          'filename': paths.basename(input.path),
          'size': size,
          'sha256': digest.toString(),
        }),
      );
    } catch (_) {
      if (await staging.exists()) await staging.delete();
      rethrow;
    }
  }

  Future<void> send() async {
    for (final item in (await store.records('photo')).entries) {
      final job = readJson(item.value);
      if (job['state'] == 'releasing') {
        await _retryRelease(item.key, job);
        continue;
      }
      if ({'uploaded', 'failed', 'released'}.contains(job['state'])) continue;
      final ledger = job['ledger_id'] as String;
      final operation = job['operation_id'] as String;
      // A tombstone still names the original posted operation for its queued receipt.
      if (await store.operationStamp(ledger, operation) == null) continue;
      try {
        await _sendOne(item.key, job);
      } on MobileHttpError catch (error) {
        if (error.status == 409 && error.code == 'upload_abandoned') {
          await _record(item.key, job, 'released');
          continue;
        }
        if (!{404, 413, 415, 422}.contains(error.status)) rethrow;
        await _record(item.key, job, 'failed');
      } on FileSystemException {
        await _record(item.key, job, 'failed');
      }
    }
  }

  Future<void> release(String ledger, String operation, String id) async {
    final saved = await store.record('photo', id);
    final job = saved == null ? null : readJson(saved);
    if (job == null ||
        job['ledger_id'] != ledger ||
        job['operation_id'] != operation) {
      throw const MobileFailure(MobileErrorCode.photoRelease);
    }
    if ({'uploaded', 'released'}.contains(job['state'])) return;
    if (job['state'] == 'queued') {
      // A reservation cannot have started before the uploading state became durable.
      await _record(id, job, 'released');
      return;
    }
    if (job['state'] != 'releasing') await _record(id, job, 'releasing');
    await _retryRelease(id, job);
  }

  Future<void> _retryRelease(String id, Json job) async {
    try {
      await _releaseOnServer(id, job);
    } on MobileHttpError catch (error) {
      if (error.status == 401 || error.code == 'csrf_failed') rethrow;
      // Keep the original request pending if the current server cannot reconcile it.
    } on IOException {
      // Continue on the next explicit retry or synchronization.
    } on TimeoutException {
      // A lost reply can be retried using the same upload identity.
    } on FormatException {
      // A receipt for another file or ledger cannot resolve this release.
    }
  }

  Future<void> _releaseOnServer(String id, Json job) async {
    final upload = '/ledgers/${job['ledger_id']}/uploads/$id';
    Object? status;
    try {
      status = await http.request('POST', '$upload/resumable/abandon');
    } on MobileHttpError catch (error) {
      if (error.status == 404 && error.code == 'not_found') {
        await _record(id, job, 'released');
        return;
      }
      if (error.status != 409 || error.code != 'upload_complete') rethrow;
      final current = await http.request('GET', upload);
      if (current is! Json ||
          current['state'] != 'ready' ||
          current['id'] != id ||
          current['ledger_id'] != job['ledger_id'] ||
          current['operation_id'] != job['operation_id'] ||
          current['declared_size'] != job['size']) {
        throw const FormatException('Sealed upload scope differs');
      }
      await _recordUploaded(id, job, current['response']);
      return;
    }
    if (status is! Json ||
        status['state'] != 'abandoned' ||
        status['upload_id'] != id ||
        status['ledger_id'] != job['ledger_id'] ||
        status['declared_size'] != job['size'] ||
        status['sha256'] != job['sha256']) {
      throw const FormatException('Abandoned upload receipt differs');
    }
    await _record(id, job, 'released');
  }

  Future<void> _record(String id, Json job, String state) =>
      store.put('photo', id, jsonEncode({...job, 'state': state}));

  Future<void> discardDraft(MobileEntry entry) async {
    for (final item in (await store.records('photo')).entries) {
      final photo = readJson(item.value);
      if (photo['ledger_id'] != entry.draft.ledgerId ||
          photo['operation_id'] != entry.id) {
        continue;
      }
      if ({'uploaded', 'releasing', 'released'}.contains(photo['state'])) {
        continue;
      }
      await store.remove('photo', item.key);
      final file = File(paths.join(directory.path, item.key));
      if (await file.exists()) await file.delete();
    }
  }

  Future<void> _sendOne(String id, Json job) async {
    final file = File(paths.join(directory.path, id));
    final size = job['size'] as int;
    if (await file.length() != size ||
        (await sha256.bind(file.openRead()).first).toString() !=
            job['sha256']) {
      throw FileSystemException('Private receipt changed');
    }
    final ledger = job['ledger_id'];
    final uploads = '/ledgers/$ledger/uploads';
    final resume = '$uploads/$id/resumable';
    final headers = {'X-Coinpup-Device-Id': deviceId};
    await _record(id, job, 'uploading');
    await http.request(
      'POST',
      uploads,
      json: {
        'id': id,
        'operation_id': job['operation_id'],
        'original_filename': job['filename'],
        'declared_size': size,
      },
    );
    var status = readObject(
      await http.request(
        'PUT',
        resume,
        headers: headers,
        json: {'sha256': job['sha256']},
      ),
    );
    var offset = _offset(status, id, job);
    if (status['state'] == 'abandoned') {
      await _record(id, job, 'released');
      return;
    }
    final chunkSize = status['max_chunk_bytes'] as int;
    if (chunkSize < 1 || chunkSize > 1024 * 1024) {
      throw const FormatException('Invalid chunk length');
    }
    final reader = await file.open();
    try {
      while (offset < size) {
        await reader.setPosition(offset);
        final count = min(chunkSize, size - offset);
        final bytes = await reader.read(count);
        if (bytes.length != count) {
          throw FileSystemException('Private receipt is incomplete');
        }
        status = readObject(
          await http.request(
            'PUT',
            '$resume/chunks/$offset',
            headers: headers,
            bytes: bytes,
          ),
        );
        final next = _offset(status, id, job);
        if (next != offset + count) {
          throw const FormatException('Unexpected upload offset');
        }
        offset = next;
      }
    } finally {
      await reader.close();
    }
    await _recordUploaded(
      id,
      job,
      await http.request('POST', '$resume/complete', headers: headers),
    );
  }

  Future<void> _recordUploaded(String id, Json job, Object? receipt) async {
    if (receipt is! Json ||
        receipt['upload_id'] != id ||
        receipt['byte_size'] != job['size'] ||
        receipt['sha256'] != job['sha256'] ||
        receipt['link_id'] == null) {
      throw const FormatException('Upload receipt differs');
    }
    await store.put(
      'photo',
      id,
      jsonEncode({...job, 'state': 'uploaded', 'receipt': receipt}),
    );
  }

  int _offset(Json status, String id, Json job) {
    final offset = status['next_offset'] as int;
    if (status['upload_id'] != id ||
        status['device_id'] != deviceId ||
        status['ledger_id'] != job['ledger_id'] ||
        status['sha256'] != job['sha256'] ||
        status['declared_size'] != job['size'] ||
        offset < 0 ||
        offset > (job['size'] as int)) {
      throw const FormatException('Upload status differs');
    }
    return offset;
  }
}
