import 'dart:convert';

import 'offline_store.dart';
import 'ui/mobile_adapter.dart';

/// Android lost-data responses carry no ledger or entry identity.
final class CameraRequest {
  CameraRequest(this.id, this.scope, this.ledger, this.entry, this.version);

  factory CameraRequest.decode(String value) {
    final data = jsonDecode(value) as Map<String, dynamic>;
    final id = data['id'] as String;
    if (!_uuid.hasMatch(id)) {
      throw const FormatException('Invalid camera request identity');
    }
    return CameraRequest(
      id,
      OfflineScope(data['server'] as String, data['owner'] as String),
      data['ledger'] as String,
      data['entry'] as String,
      data['version'] as int,
    )..path = data['path'] as String?;
  }

  static final _uuid = RegExp(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
  );
  final String id;
  final OfflineScope scope;
  final String ledger;
  final String entry;
  final int version;
  String? path;

  bool owns(OfflineScope other) =>
      scope.authority == other.authority && scope.ownerId == other.ownerId;

  MobileEntry? current(MobileSnapshot view) {
    if (!view.ledgers.any((row) => row.id == ledger && !row.archived)) {
      return null;
    }
    return view.entries
        .where(
          (row) =>
              row.ledgerId == ledger &&
              row.id == entry &&
              row.version == version &&
              row.canAttachPhoto,
        )
        .firstOrNull;
  }

  String encode() => jsonEncode({
    'id': id,
    'server': scope.authority,
    'owner': scope.ownerId,
    'ledger': ledger,
    'entry': entry,
    'version': version,
    'path': path,
  });
}
