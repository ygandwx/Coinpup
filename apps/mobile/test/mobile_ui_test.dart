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
  final conflicts = <MobileConflict>[];
  Uri? signedInto;
  MobileDraft? savedDraft;
  ConflictChoice? choice;
  bool signedIn = false;
  int offset = 0;

  MobileSnapshot get snapshot => MobileSnapshot(
    ownerLabel: 'Fictional device owner',
    ledgers: const [alpha, beta],
    assets: const [usd],
    entries: List.of(entries),
    conflicts: List.of(conflicts),
    pendingCount: entries.length,
    syncStatus: SyncStatus.offline,
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
    await tester.pumpWidget(CoinpupApp(adapter: fixture));
    await tester.pumpAndSettle();
  }

  testWidgets(
    'sign in, switch language in a draft, edit and remove an offline entry',
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

  testWidgets(
    'a conflict keeps both sides and a deletion cannot be resurrected',
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
