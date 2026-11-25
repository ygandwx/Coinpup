import 'package:coinpup_mobile/ui/coinpup_app.dart';
import 'package:coinpup_mobile/ui/mobile_adapter.dart';
import 'package:material_ui/material_ui.dart';
import 'package:flutter_test/flutter_test.dart';

const usd = MobileAsset(id: 'USD', code: 'USD', scale: 2);
const alpha = MobileLedger(
  id: 'alpha',
  name: 'Fictional Alpha',
  accounts: [
    MobileAccount(id: 'bank', name: 'Fictional bank', assetIds: ['USD']),
  ],
  archivedAccountNames: {'old-bank': 'Fictional older bank'},
  categories: [
    MobileCategory(
      id: 'supplies',
      name: 'Fictional supplies',
      kind: EntryKind.expense,
    ),
  ],
);
const beta = MobileLedger(id: 'beta', name: 'Fictional Beta');
const localConflict = MobileDraft(
  ledgerId: 'alpha',
  kind: EntryKind.expense,
  accountId: 'bank',
  categoryId: 'supplies',
  assetId: 'USD',
  amount: '12.30',
  date: '2026-11-21',
  description: 'Fictional local expense',
);
const serverConflict = MobileDraft(
  ledgerId: 'alpha',
  kind: EntryKind.expense,
  accountId: 'bank',
  categoryId: 'supplies',
  assetId: 'USD',
  amount: '13.00',
  date: '2026-11-21',
  description: 'Fictional server revision',
);

class FixtureAdapter implements MobileAdapter {
  final entries = <MobileEntry>[];
  final readOnlyPostings = <MobileReadOnlyPosting>[];
  final detachedReceipts = <DetachedReceipt>[];
  final conflicts = <MobileConflict>[];
  Uri? signedInto;
  MobileDraft? savedDraft;
  String? receiptPath;
  final releaseRequests = <String>[];
  bool leaveReleasePending = false;
  ConflictChoice? choice;
  bool signedIn = false;
  @override
  bool cameraRecoveryFailed = false;
  int offset = 0;

  MobileSnapshot get snapshot => MobileSnapshot(
    ownerLabel: 'Fictional device owner',
    ledgers: const [alpha, beta],
    assets: const [usd],
    entries: List.of(entries),
    readOnlyPostings: List.of(readOnlyPostings),
    detachedReceipts: List.of(detachedReceipts),
    conflicts: List.of(conflicts),
    pendingCount: entries.length,
    syncStatus: SyncStatus.offline,
    quotes: const [
      MobileQuote(
        assetId: 'USDT:fictional',
        displayAssetId: 'USD',
        priceDate: '2026-11-20',
        observedAt: '2026-11-20T08:09:10Z',
        cachedAt: '2026-11-20T08:12:13Z',
        price: '0.990000000000000001',
        source: 'Fictional manual source',
      ),
      MobileQuote(
        assetId: 'EUR',
        displayAssetId: 'USD',
        priceDate: '2026-11-20',
      ),
      MobileQuote(
        assetId: 'GBP',
        displayAssetId: 'USD',
        priceDate: '2026-11-20',
        price: '1.2',
        cachedAt: '2026-11-20T08:25:30Z',
        source: 'Fictional date-only source',
      ),
    ],
  );

  @override
  Future<MobileSnapshot?> restore() async => signedIn ? snapshot : null;

  @override
  Future<MobileSnapshot> signIn(
    Uri server,
    String username,
    String password,
  ) async {
    signedInto = server;
    signedIn = true;
    return snapshot;
  }

  @override
  Future<void> signOut() async => signedIn = false;

  @override
  Future<MobileSnapshot> save(
    MobileDraft draft, {
    MobileEntry? previous,
  }) async {
    savedDraft = draft;
    final row = MobileEntry(
      id: previous?.id ?? 'fixture-${++offset}',
      version: (previous?.version ?? 0) + 1,
      draft: draft,
      lifecycle: EntryLifecycle.queued,
      canEdit: true,
      canDelete: true,
      canAttachPhoto: true,
      attachments: previous?.attachments ?? [],
    );
    if (previous != null) entries.removeWhere((item) => item.id == previous.id);
    entries.add(row);
    return snapshot;
  }

  @override
  Future<MobileSnapshot> delete(MobileEntry entry) async {
    entries.removeWhere((item) => item.id == entry.id);
    return snapshot;
  }

  @override
  Future<MobileSnapshot> capturePhoto(
    MobileEntry entry,
    CaptureReceipt capture,
  ) async {
    final temporaryPath = await capture();
    if (temporaryPath == null) return snapshot;
    receiptPath = temporaryPath;
    return _withAttachments(entry, [
      ...entry.attachments,
      const MobileAttachment('photo-1', 'receipt.jpg', AttachmentState.queued),
    ]);
  }

  @override
  Future<MobileSnapshot> releasePhoto(
    String ledger,
    String operation,
    String uploadId,
  ) async {
    final entry = entries
        .where((item) => item.ledgerId == ledger && item.id == operation)
        .firstOrNull;
    releaseRequests.add(uploadId);
    if (entry == null) {
      final index = detachedReceipts.indexWhere(
        (row) =>
            row.ledgerId == ledger &&
            row.operationId == operation &&
            row.attachment.id == uploadId,
      );
      final row = detachedReceipts[index];
      detachedReceipts[index] = (
        ledgerId: row.ledgerId,
        operationId: row.operationId,
        attachment: MobileAttachment(
          uploadId,
          row.attachment.filename,
          AttachmentState.released,
        ),
      );
      return snapshot;
    }
    return _withAttachments(entry, [
      for (final photo in entry.attachments)
        if (photo.id == uploadId)
          MobileAttachment(
            photo.id,
            photo.filename,
            leaveReleasePending
                ? AttachmentState.releasing
                : AttachmentState.released,
          )
        else
          photo,
    ]);
  }

  MobileSnapshot _withAttachments(
    MobileEntry entry,
    List<MobileAttachment> photos,
  ) {
    final index = entries.indexWhere((item) => item.id == entry.id);
    entries[index] = MobileEntry(
      id: entry.id,
      version: entry.version,
      draft: entry.draft,
      lifecycle: entry.lifecycle,
      canEdit: entry.canEdit,
      canDelete: entry.canDelete,
      canAttachPhoto: entry.canAttachPhoto,
      attachments: photos,
    );
    return snapshot;
  }

  @override
  Future<MobileSnapshot> synchronize() async => snapshot;

  @override
  Future<MobileSnapshot> resolveConflict(
    String id,
    ConflictChoice selected,
  ) async {
    choice = selected;
    conflicts.removeWhere((item) => item.id == id);
    return snapshot;
  }
}

void main() {
  Finder key(String value) => find.byKey(ValueKey(value));

  Finder scroll(String list) =>
      find.descendant(of: key(list), matching: find.byType(Scrollable)).first;

  Future<void> tap(WidgetTester tester, Finder target) async {
    await tester.ensureVisible(target.first);
    await tester.pumpAndSettle();
    if (target.hitTestable().evaluate().isEmpty &&
        scroll('workspace-scroll').evaluate().isNotEmpty) {
      await tester.scrollUntilVisible(
        target.first,
        180,
        scrollable: scroll('workspace-scroll'),
      );
    }
    await tester.tap(target.first);
    await tester.pumpAndSettle();
  }

  Future<void> start(WidgetTester tester, FixtureAdapter fixture) async {
    tester.view.physicalSize = const Size(320, 740);
    tester.view.devicePixelRatio = 1;
    tester.binding.platformDispatcher.localeTestValue = const Locale('en');
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
      tester.binding.platformDispatcher.clearLocaleTestValue();
    });
    await tester.pumpWidget(
      CoinpupApp(
        adapter: fixture,
        captureReceipt: () async => '/fixture/receipt.jpg',
      ),
    );
    await tester.pumpAndSettle();
  }

  testWidgets('unmatched camera recovery asks for a retake in both languages', (
    tester,
  ) async {
    final fixture = FixtureAdapter()..cameraRecoveryFailed = true;
    await start(tester, fixture);
    expect(find.byKey(const ValueKey('sign-in')), findsOneWidget);
    expect(find.textContaining('Open the intended entry'), findsOneWidget);
    await tap(tester, key('app-language'));
    expect(find.textContaining('请打开对应的记录重新拍摄'), findsOneWidget);
    expect(fixture.receiptPath, isNull);
  });

  testWidgets('release confirms one named receipt and hides sealed controls', (
    tester,
  ) async {
    final fixture = FixtureAdapter()..signedIn = true;
    fixture.entries.add(
      const MobileEntry(
        id: 'one-entry',
        version: 1,
        draft: localConflict,
        lifecycle: EntryLifecycle.posted,
        attachments: [
          MobileAttachment(
            'one',
            'fictional-first.jpg',
            AttachmentState.failed,
          ),
          MobileAttachment(
            'two',
            'fictional-second.jpg',
            AttachmentState.queued,
          ),
          MobileAttachment(
            'sealed',
            'fictional-sealed.jpg',
            AttachmentState.uploaded,
          ),
        ],
      ),
    );
    await start(tester, fixture);
    await tap(tester, find.text('12.30 USD'));

    Finder action(String id, String label) =>
        find.descendant(of: key('receipt-$id'), matching: find.text(label));

    expect(action('sealed', 'Release upload'), findsNothing);
    await tap(tester, action('one', 'Release upload'));
    expect(
      find.text('Release upload for fictional-first.jpg?'),
      findsOneWidget,
    );
    await tap(tester, find.text('Back'));
    expect(fixture.releaseRequests, isEmpty);

    await tap(tester, key('app-language'));
    fixture.leaveReleasePending = true;
    await tap(tester, action('one', '释放上传'));
    expect(find.text('票据原件将保留在此设备上。'), findsOneWidget);
    await tap(tester, find.widgetWithText(FilledButton, '释放上传'));
    expect(fixture.releaseRequests, ['one']);
    expect(action('one', '等待释放上传。请在联网后重试。'), findsOneWidget);

    fixture.leaveReleasePending = false;
    await tap(tester, action('one', '重试释放'));
    await tap(tester, find.widgetWithText(FilledButton, '释放上传'));
    expect(fixture.releaseRequests, ['one', 'one']);
    expect(action('one', '上传已释放。原件已保留在此设备上。'), findsOneWidget);
    expect(action('one', '释放上传'), findsNothing);
    expect(action('two', '释放上传'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('a hidden entry still exposes its own queued receipt release', (
    tester,
  ) async {
    final fixture = FixtureAdapter()..signedIn = true;
    fixture.detachedReceipts.addAll(const [
      (
        ledgerId: 'alpha',
        operationId: 'canceled',
        attachment: MobileAttachment(
          'detached',
          'fictional-detached.jpg',
          AttachmentState.uploading,
        ),
      ),
      (
        ledgerId: 'beta',
        operationId: 'another',
        attachment: MobileAttachment(
          'another',
          'fictional-other-ledger.jpg',
          AttachmentState.queued,
        ),
      ),
    ]);
    await start(tester, fixture);
    expect(key('entry-canceled'), findsNothing);
    expect(find.text('fictional-other-ledger.jpg'), findsNothing);
    expect(find.text('Receipts outside current entries'), findsOneWidget);
    await tap(tester, find.text('Release upload'));
    expect(
      find.text('Release upload for fictional-detached.jpg?'),
      findsOneWidget,
    );
    await tap(tester, find.widgetWithText(FilledButton, 'Release upload'));
    expect(fixture.releaseRequests, ['detached']);
    expect(
      find.text('Upload released. Original kept on this device.'),
      findsOneWidget,
    );
    expect(find.text('Release upload'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'sign in, switch language in a draft, attach and remove an offline entry',
    (tester) async {
      final fixture = FixtureAdapter();
      await start(tester, fixture);
      await tester.enterText(key('server'), 'https://fixture.invalid');
      await tester.enterText(key('username'), 'fictional');
      await tester.enterText(key('password'), 'fictional-password');
      await tap(tester, key('sign-in'));
      expect(fixture.signedInto, Uri.parse('https://fixture.invalid'));
      expect(find.text('Working offline'), findsOneWidget);

      await tap(tester, key('new-entry'));
      await tester.enterText(key('entry-amount'), '0.001');
      await tester.scrollUntilVisible(
        key('save-entry'),
        170,
        scrollable: scroll('entry-form-scroll'),
      );
      await tap(tester, key('save-entry'));
      expect(fixture.savedDraft, isNull);
      await tester.scrollUntilVisible(
        key('entry-amount'),
        -150,
        scrollable: scroll('entry-form-scroll'),
      );
      expect(
        find.text('Enter a positive decimal in the asset precision.'),
        findsOneWidget,
      );
      await tester.enterText(key('entry-amount'), '12.30');
      await tap(tester, key('app-language'));
      expect(find.text('新增记录'), findsOneWidget);
      expect(
        tester.widget<TextFormField>(key('entry-amount')).controller!.text,
        '12.30',
      );
      await tester.scrollUntilVisible(
        key('save-entry'),
        170,
        scrollable: scroll('entry-form-scroll'),
      );
      await tap(tester, key('save-entry'));
      expect(fixture.savedDraft?.amount, '12.30');
      expect(find.text('12.30 USD'), findsOneWidget);

      await tap(tester, key('app-language'));
      await tap(tester, find.text('12.30 USD'));
      await tap(tester, find.text('Photograph receipt'));
      expect(fixture.receiptPath, '/fixture/receipt.jpg');
      expect(find.text('Receipt queued'), findsOneWidget);
      await tap(tester, find.text('Edit entry'));
      await tester.enterText(key('entry-amount'), '12.40');
      await tester.scrollUntilVisible(
        key('save-entry'),
        170,
        scrollable: scroll('entry-form-scroll'),
      );
      await tap(tester, key('save-entry'));
      expect(fixture.savedDraft?.amount, '12.40');
      if (find.text('Delete draft').evaluate().isEmpty) {
        await tap(tester, find.text('12.40 USD'));
      }
      await tap(tester, find.text('Delete draft'));
      expect(find.text('Delete this draft?'), findsOneWidget);
      await tap(tester, find.widgetWithText(FilledButton, 'Delete draft'));
      expect(fixture.entries, isEmpty);
      expect(find.text('No entries in this ledger yet.'), findsOneWidget);
      await tap(tester, key('ledger'));
      await tap(tester, find.text('Fictional Beta').last);
      expect(find.text('Fictional Beta'), findsOneWidget);
      expect(
        tester.widget<FloatingActionButton>(key('new-entry')).onPressed,
        isNull,
      );
      await tap(tester, find.byTooltip('Sign out'));
      expect(find.text('Fictional device owner'), findsNothing);
      expect(find.text('Sign in to your server'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('read-only posting details retain both legs at phone width', (
    tester,
  ) async {
    final fixture = FixtureAdapter()..signedIn = true;
    fixture.readOnlyPostings.addAll(const [
      MobileReadOnlyPosting(
        id: 'opening',
        ledgerId: 'alpha',
        kind: ReadOnlyPostingKind.opening,
        primary: (accountId: 'bank', assetId: 'USD', amount: '-25.00'),
        date: '2026-11-20',
        recognitionDate: '2026-11-20',
        description: 'Fictional opening balance',
      ),
      MobileReadOnlyPosting(
        id: 'transfer',
        ledgerId: 'alpha',
        kind: ReadOnlyPostingKind.transfer,
        primary: (accountId: 'bank', assetId: 'USD', amount: '31.01'),
        destination: (accountId: 'old-bank', assetId: 'USD', amount: '31.01'),
        date: '2026-11-21',
        recognitionDate: '2026-11-21',
        description: '',
      ),
      MobileReadOnlyPosting(
        id: 'fx',
        ledgerId: 'alpha',
        kind: ReadOnlyPostingKind.exchange,
        primary: (
          accountId: 'bank',
          assetId: 'token:chain-a:FictionalUnit',
          amount: '1.000000000000000001',
        ),
        destination: (
          accountId: 'bank',
          assetId: 'token:chain-b:FictionalUnit',
          amount: '0.999999999999999998',
        ),
        date: '2026-11-22',
        recognitionDate: '2026-11-23',
        description: '',
        fees: [
          (
            quantity: (accountId: 'old-bank', assetId: 'USD', amount: '0.01'),
            categoryId: 'supplies',
          ),
        ],
      ),
    ]);
    await start(tester, fixture);
    expect(find.text('Working offline'), findsOneWidget);
    final fx = key('read-only-posting-fx');
    await tap(tester, find.text('Currency exchange'));
    expect(
      find.text('Sent 1.000000000000000001 token:chain-a:FictionalUnit'),
      findsOneWidget,
    );
    expect(
      find.text('Received 0.999999999999999998 token:chain-b:FictionalUnit'),
      findsOneWidget,
    );
    expect(find.text('Recognition date 2026-11-23'), findsOneWidget);
    expect(
      find.text('Fee 0.01 USD · Fictional older bank · Fictional supplies'),
      findsOneWidget,
    );
    expect(
      find.descendant(of: fx, matching: find.byType(TextButton)),
      findsNothing,
    );
    await tap(tester, key('app-language'));
    expect(find.text('换汇'), findsOneWidget);
    expect(
      find.text('转出 1.000000000000000001 token:chain-a:FictionalUnit'),
      findsOneWidget,
    );
    expect(
      find.text('转入 0.999999999999999998 token:chain-b:FictionalUnit'),
      findsOneWidget,
    );
    expect(find.text('仅供查看'), findsOneWidget);
    await tap(tester, find.text('换汇'));
    await tap(tester, find.text('转账'));
    expect(find.text('转出 31.01 USD'), findsOneWidget);
    expect(find.text('转入 31.01 USD'), findsOneWidget);
    expect(find.text('转入账户 Fictional older bank'), findsOneWidget);
    await tap(tester, find.text('转账'));
    await tap(tester, find.text('期初余额'));
    expect(find.text('-25.00 USD'), findsOneWidget);
    expect(find.text('账户 Fictional bank'), findsOneWidget);
    expect(find.text('取消记录'), findsNothing);
    expect(find.text('编辑记录'), findsNothing);
    expect(find.text('拍摄票据'), findsNothing);
    await tap(tester, key('ledger'));
    await tap(tester, find.text('Fictional Beta').last);
    expect(find.text('此账本暂无记录。'), findsOneWidget);
    expect(find.text('期初余额'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'quotes and exact source time are visible and a deletion cannot be resurrected',
    (tester) async {
      final fixture = FixtureAdapter()..signedIn = true;
      fixture.conflicts.addAll(const [
        MobileConflict(
          id: 'live',
          ledgerId: 'alpha',
          submitted: localConflict,
          current: serverConflict,
          submittedVersion: 1,
          currentVersion: 2,
          canKeepLocal: true,
        ),
        MobileConflict(
          id: 'deleted',
          ledgerId: 'alpha',
          submitted: localConflict,
          current: null,
          submittedVersion: 2,
          currentVersion: 3,
          serverDeleted: true,
          canKeepLocal: true,
        ),
      ]);
      await start(tester, fixture);
      await tap(tester, find.text('Quotes'));
      expect(find.text('0.990000000000000001'), findsOneWidget);
      expect(find.text('Observed at 2026-11-20T08:09:10Z'), findsOneWidget);
      expect(find.text('Price unavailable'), findsOneWidget);
      expect(find.text('Cached at 2026-11-20T08:12:13Z'), findsOneWidget);
      expect(find.text('Cached at 2026-11-20T08:25:30Z'), findsOneWidget);
      expect(find.text('Observed at 2026-11-20T08:25:30Z'), findsNothing);
      await tap(tester, key('app-language'));
      expect(find.text('观察时间 2026-11-20T08:09:10Z'), findsOneWidget);
      expect(find.text('缓存时间 2026-11-20T08:12:13Z'), findsOneWidget);
      expect(find.text('缓存时间 2026-11-20T08:25:30Z'), findsOneWidget);
      expect(find.text('观察时间 2026-11-20T08:25:30Z'), findsNothing);
      await tap(tester, key('app-language'));
      await tap(tester, find.text('Sync'));
      await tap(tester, key('sync-now'));
      await tap(tester, key('conflict-live'));
      expect(find.text('Fictional local expense'), findsOneWidget);
      expect(find.text('Fictional server revision'), findsOneWidget);
      await tap(tester, find.text('Submit local changes as a new revision'));
      expect(
        find.text('Submit the local changes as a new revision?'),
        findsOneWidget,
      );
      await tap(
        tester,
        find.widgetWithText(FilledButton, 'Submit new revision'),
      );
      expect(fixture.choice, ConflictChoice.submitLocal);
      await tap(tester, key('conflict-deleted'));
      expect(find.text('Deleted on the server'), findsOneWidget);
      expect(find.text('Submit local changes as a new revision'), findsNothing);
      await tap(tester, find.text('Keep server version'));
      await tap(
        tester,
        find.widgetWithText(FilledButton, 'Keep server version'),
      );
      expect(fixture.choice, ConflictChoice.keepCurrent);
      expect(tester.takeException(), isNull);
    },
  );
}
