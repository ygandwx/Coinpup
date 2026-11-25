/// Copy-safe data for the active server and owner on this device.
abstract interface class MobileAdapter {
  Future<MobileSnapshot?> restore();

  Future<MobileSnapshot> signIn(Uri server, String username, String password);

  Future<void> signOut();

  Future<MobileSnapshot> save(MobileDraft draft, {MobileEntry? previous});

  Future<MobileSnapshot> delete(MobileEntry entry);

  Future<MobileSnapshot> synchronize();

  Future<MobileSnapshot> resolveConflict(String id, ConflictChoice choice);
}

enum EntryKind { income, expense }

enum EntryLifecycle { queued, posted, conflict }

enum SyncStatus {
  ready,
  offline,
  pending,
  synced,
  signInRequired,
  reviewRequired,
}

enum ConflictChoice { keepCurrent, submitLocal }

enum MobileErrorCode {
  connection,
  signIn,
  serverAddress,
  operation,
  sync,
  conflict,
}

/// Use stable codes so private protocol errors never appear in the UI.
final class MobileFailure implements Exception {
  const MobileFailure(this.code);

  final MobileErrorCode code;
}

final class MobileAsset {
  const MobileAsset({
    required this.id,
    required this.code,
    required this.scale,
  });

  final String id;
  final String code;
  final int scale;
}

final class MobileAccount {
  const MobileAccount({
    required this.id,
    required this.name,
    required this.assetIds,
  });

  final String id;
  final String name;
  final List<String> assetIds;
}

final class MobileCategory {
  const MobileCategory({
    required this.id,
    required this.name,
    required this.kind,
  });

  final String id;
  final String name;
  final EntryKind kind;
}

final class MobileLedger {
  const MobileLedger({
    required this.id,
    required this.name,
    this.accounts = const [],
    this.categories = const [],
    this.archivedAccountNames = const {},
    this.archived = false,
  });

  final String id;
  final String name;
  final List<MobileAccount> accounts;
  final List<MobileCategory> categories;
  final Map<String, String> archivedAccountNames;
  final bool archived;
}

final class MobileDraft {
  const MobileDraft({
    required this.ledgerId,
    required this.kind,
    required this.accountId,
    required this.categoryId,
    required this.assetId,
    required this.amount,
    required this.date,
    required this.description,
  });

  final String ledgerId;
  final EntryKind kind;
  final String accountId;
  final String categoryId;
  final String assetId;
  final String amount;
  final String date;
  final String description;
}

final class MobileEntry {
  const MobileEntry({
    required this.id,
    required this.version,
    required this.draft,
    required this.lifecycle,
    this.canEdit = false,
    this.canDelete = false,
  });

  final String id;
  final int version;
  final MobileDraft draft;
  final EntryLifecycle lifecycle;
  final bool canEdit;
  final bool canDelete;

  String get ledgerId => draft.ledgerId;

  String get date => draft.date;
}

final class MobileConflict {
  const MobileConflict({
    required this.id,
    required this.ledgerId,
    required this.submitted,
    required this.current,
    this.submittedVersion,
    this.currentVersion,
    this.latest,
    this.latestVersion,
    this.serverDeleted = false,
    this.latestDeleted = false,
    this.canKeepLocal = false,
  });

  final String id;
  final String ledgerId;
  final MobileDraft? submitted;
  final MobileDraft? current;
  final int? submittedVersion;
  final int? currentVersion;
  final MobileDraft? latest;
  final int? latestVersion;
  final bool serverDeleted;
  final bool latestDeleted;
  final bool canKeepLocal;
}

final class MobileSnapshot {
  const MobileSnapshot({
    required this.ownerLabel,
    this.ledgers = const [],
    this.assets = const [],
    this.entries = const [],
    this.conflicts = const [],
    this.syncStatus = SyncStatus.ready,
    this.pendingCount = 0,
    this.lastSyncedAt,
  });

  final String ownerLabel;
  final List<MobileLedger> ledgers;
  final List<MobileAsset> assets;
  final List<MobileEntry> entries;
  final List<MobileConflict> conflicts;
  final SyncStatus syncStatus;
  final int pendingCount;
  final String? lastSyncedAt;
}
