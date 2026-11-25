import 'dart:async';

import 'package:material_ui/material_ui.dart';

import 'entry_editor.dart';
import 'mobile_adapter.dart';
import 'mobile_text.dart';

bool _canSubmitLocal(MobileConflict conflict) =>
    conflict.canKeepLocal &&
    !conflict.serverDeleted &&
    !conflict.latestDeleted &&
    conflict.submitted?.ledgerId == conflict.ledgerId &&
    (conflict.latest ?? conflict.current)?.ledgerId == conflict.ledgerId;

class CoinpupApp extends StatefulWidget {
  const CoinpupApp({required this.adapter, super.key});

  final MobileAdapter adapter;

  @override
  State<CoinpupApp> createState() => _CoinpupAppState();
}

class _CoinpupAppState extends State<CoinpupApp> {
  final navigator = GlobalKey<NavigatorState>();
  MobileSnapshot? snapshot;
  MobileErrorCode? error;
  Locale? locale;
  String? selectedLedgerId;
  int tab = 0;
  bool restoring = true;
  bool busy = false;

  @override
  void initState() {
    super.initState();
    unawaited(_restore());
  }

  Future<void> _restore() async {
    try {
      final restored = await widget.adapter.restore();
      if (mounted) setState(() => snapshot = restored);
    } catch (_) {
      if (mounted) setState(() => error = MobileErrorCode.operation);
    } finally {
      if (mounted) setState(() => restoring = false);
    }
  }

  void _changeLanguage() {
    final context = navigator.currentContext;
    final selected =
        locale ??
        (context == null
            ? const Locale('en')
            : Localizations.localeOf(context));
    setState(
      () => locale = Locale(selected.languageCode == 'zh' ? 'en' : 'zh'),
    );
  }

  Future<bool> _run(
    Future<MobileSnapshot?> Function() action,
    MobileErrorCode fallback,
  ) async {
    if (busy) return false;
    setState(() {
      busy = true;
      error = null;
    });
    try {
      final updated = await action();
      if (!mounted) return false;
      if (updated != null) setState(() => snapshot = updated);
      return true;
    } on MobileFailure catch (failure) {
      if (mounted) setState(() => error = failure.code);
      return false;
    } catch (_) {
      if (mounted) setState(() => error = fallback);
      return false;
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  Future<void> _signOut() async {
    if (busy) return;
    setState(() {
      snapshot = null;
      selectedLedgerId = null;
      tab = 0;
    });
    await _run(() async {
      await widget.adapter.signOut();
      return null;
    }, MobileErrorCode.signIn);
  }

  Future<void> _edit(
    BuildContext context,
    MobileLedger ledger, [
    MobileEntry? entry,
  ]) async {
    final current = snapshot;
    if (busy ||
        ledger.archived ||
        current == null ||
        (entry != null && !entry.canEdit)) {
      return;
    }
    await Navigator.of(context).push<void>(
      MaterialPageRoute(
        builder: (_) => EntryEditor(
          ledger: ledger,
          assets: current.assets,
          previous: entry,
          changeLanguage: _changeLanguage,
          save: (draft) => _run(
            () => widget.adapter.save(draft, previous: entry),
            MobileErrorCode.operation,
          ),
        ),
      ),
    );
  }

  Future<bool> _confirm(
    BuildContext context,
    String title,
    String action,
  ) async =>
      await showDialog<bool>(
        context: context,
        builder: (dialog) => AlertDialog(
          title: Text(title),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialog, false),
              child: Text(uiText(dialog, 'Back', '返回')),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(dialog, true),
              child: Text(action),
            ),
          ],
        ),
      ) ??
      false;

  Future<void> _delete(BuildContext context, MobileEntry entry) async {
    if (busy || !entry.canDelete) return;
    final posted = entry.lifecycle == EntryLifecycle.posted;
    final confirmed = await _confirm(
      context,
      posted
          ? uiText(context, 'Cancel this posted entry?', '取消这笔已入账记录？')
          : uiText(context, 'Delete this draft?', '删除这笔草稿？'),
      posted
          ? uiText(context, 'Cancel entry', '取消记录')
          : uiText(context, 'Delete draft', '删除草稿'),
    );
    if (confirmed && mounted) {
      await _run(() => widget.adapter.delete(entry), MobileErrorCode.operation);
    }
  }

  Future<void> _resolve(
    BuildContext context,
    MobileConflict conflict,
    ConflictChoice choice,
  ) async {
    if (busy ||
        (choice == ConflictChoice.submitLocal && !_canSubmitLocal(conflict))) {
      return;
    }
    final useLocal = choice == ConflictChoice.submitLocal;
    final action = useLocal
        ? uiText(context, 'Submit new revision', '提交新修订')
        : uiText(context, 'Keep server version', '保留服务器版本');
    final confirmed = await _confirm(
      context,
      useLocal
          ? uiText(
              context,
              'Submit the local changes as a new revision?',
              '将本地改动作为新修订提交？',
            )
          : uiText(
              context,
              'Keep the server version for this conflict?',
              '对此冲突保留服务器版本？',
            ),
      action,
    );
    if (confirmed && mounted) {
      await _run(
        () => widget.adapter.resolveConflict(conflict.id, choice),
        MobileErrorCode.conflict,
      );
    }
  }

  @override
  Widget build(BuildContext context) => MaterialApp(
    navigatorKey: navigator,
    title: 'Coinpup',
    debugShowCheckedModeBanner: false,
    locale: locale,
    supportedLocales: const [Locale('en'), Locale('zh')],
    localizationsDelegates: GlobalMaterialLocalizations.delegates,
    theme: ThemeData(
      colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xff485b2d)),
      scaffoldBackgroundColor: const Color(0xfffaf9f3),
      inputDecorationTheme: const InputDecorationTheme(
        border: OutlineInputBorder(),
      ),
    ),
    home: Builder(
      builder: (context) {
        final current = snapshot;
        if (restoring) {
          return const Scaffold(
            body: Center(child: CircularProgressIndicator()),
          );
        }
        if (current == null ||
            current.syncStatus == SyncStatus.signInRequired) {
          return _SignInPage(
            busy: busy,
            error: error ?? (current == null ? null : MobileErrorCode.signIn),
            changeLanguage: _changeLanguage,
            onSignIn: (server, username, password) => _run(
              () => widget.adapter.signIn(server, username, password),
              MobileErrorCode.signIn,
            ),
          );
        }
        final ledger =
            current.ledgers
                .where((item) => item.id == selectedLedgerId)
                .firstOrNull ??
            current.ledgers.firstOrNull;
        return Scaffold(
          appBar: AppBar(
            title: const Text('Coinpup'),
            actions: [
              LanguageButton(onPressed: _changeLanguage),
              IconButton(
                onPressed: busy ? null : _signOut,
                tooltip: uiText(context, 'Sign out', '退出登录'),
                icon: const Icon(Icons.logout),
              ),
            ],
          ),
          body: SafeArea(
            child: ListView(
              key: const ValueKey('workspace-scroll'),
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 88),
              children: [
                _AccountStatus(snapshot: current, busy: busy, error: error),
                const SizedBox(height: 14),
                if (tab == 0) ...[
                  if (ledger == null)
                    Text(
                      uiText(
                        context,
                        'No ledgers on this device yet.',
                        '此设备暂无账本。',
                      ),
                    )
                  else ...[
                    InputDecorator(
                      decoration: InputDecoration(
                        labelText: uiText(context, 'Ledger', '账本'),
                      ),
                      child: DropdownButtonHideUnderline(
                        child: DropdownButton<String>(
                          key: const ValueKey('ledger'),
                          isExpanded: true,
                          isDense: true,
                          value: ledger.id,
                          items: [
                            for (final item in current.ledgers)
                              DropdownMenuItem<String>(
                                value: item.id,
                                child: Text(
                                  item.archived
                                      ? '${item.name} · ${uiText(context, 'Archived', '已归档')}'
                                      : item.name,
                                  overflow: TextOverflow.ellipsis,
                                ),
                              ),
                          ],
                          onChanged: busy
                              ? null
                              : (value) =>
                                    setState(() => selectedLedgerId = value),
                        ),
                      ),
                    ),
                    _Entries(
                      ledger: ledger,
                      records: current.entries
                          .where((row) => row.ledgerId == ledger.id)
                          .toList(),
                      locked: busy,
                      edit: (entry) => _edit(context, ledger, entry),
                      delete: (entry) => _delete(context, entry),
                    ),
                  ],
                ] else
                  _SyncView(
                    snapshot: current,
                    busy: busy,
                    sync: () =>
                        _run(widget.adapter.synchronize, MobileErrorCode.sync),
                    resolve: (conflict, choice) =>
                        _resolve(context, conflict, choice),
                  ),
              ],
            ),
          ),
          floatingActionButton: tab != 0 || ledger == null || ledger.archived
              ? null
              : FloatingActionButton.extended(
                  key: const ValueKey('new-entry'),
                  onPressed:
                      busy ||
                          ledger.accounts.isEmpty ||
                          ledger.categories.isEmpty
                      ? null
                      : () => _edit(context, ledger),
                  icon: const Icon(Icons.add),
                  label: Text(uiText(context, 'New entry', '新增记录')),
                ),
          bottomNavigationBar: NavigationBar(
            selectedIndex: tab,
            onDestinationSelected: busy
                ? null
                : (value) => setState(() => tab = value),
            destinations: [
              NavigationDestination(
                icon: const Icon(Icons.list_alt_outlined),
                label: uiText(context, 'Entries', '记录'),
              ),
              NavigationDestination(
                icon: const Icon(Icons.sync),
                label: uiText(context, 'Sync', '同步'),
              ),
            ],
          ),
        );
      },
    ),
  );
}

class _SignInPage extends StatefulWidget {
  const _SignInPage({
    required this.busy,
    required this.error,
    required this.changeLanguage,
    required this.onSignIn,
  });

  final bool busy;
  final MobileErrorCode? error;
  final VoidCallback changeLanguage;
  final Future<bool> Function(Uri, String, String) onSignIn;

  @override
  State<_SignInPage> createState() => _SignInPageState();
}

class _SignInPageState extends State<_SignInPage> {
  final form = GlobalKey<FormState>();
  final server = TextEditingController();
  final username = TextEditingController();
  final password = TextEditingController();

  @override
  void dispose() {
    server.dispose();
    username.dispose();
    password.dispose();
    super.dispose();
  }

  Uri? _address(String? text) {
    final uri = Uri.tryParse(text?.trim() ?? '');
    if (uri == null ||
        !['https', 'http'].contains(uri.scheme) ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        uri.hasQuery ||
        uri.hasFragment ||
        (uri.path.isNotEmpty && uri.path != '/')) {
      return null;
    }
    return uri;
  }

  Future<void> _submit() async {
    if (widget.busy || !form.currentState!.validate()) return;
    final uri = _address(server.text)!;
    final secret = password.text;
    password.clear();
    await widget.onSignIn(uri, username.text.trim(), secret);
  }

  @override
  Widget build(BuildContext context) {
    String t(String english, String chinese) =>
        uiText(context, english, chinese);
    return Scaffold(
      appBar: AppBar(
        title: const Text('Coinpup'),
        actions: [LanguageButton(onPressed: widget.changeLanguage)],
      ),
      body: SafeArea(
        child: Form(
          key: form,
          child: ListView(
            padding: const EdgeInsets.all(20),
            children: [
              Text(
                t('Sign in to your server', '登录你的服务器'),
                style: Theme.of(context).textTheme.headlineSmall,
              ),
              const SizedBox(height: 20),
              TextFormField(
                key: const ValueKey('server'),
                controller: server,
                enabled: !widget.busy,
                keyboardType: TextInputType.url,
                autocorrect: false,
                decoration: InputDecoration(
                  labelText: t('Server address', '服务器地址'),
                  hintText: 'https://',
                ),
                validator: (value) => _address(value) == null
                    ? errorLabel(context, MobileErrorCode.serverAddress)
                    : null,
              ),
              const SizedBox(height: 12),
              TextFormField(
                key: const ValueKey('username'),
                controller: username,
                enabled: !widget.busy,
                autocorrect: false,
                decoration: InputDecoration(labelText: t('Username', '用户名')),
                validator: (value) => (value ?? '').trim().isEmpty
                    ? t('Enter your username.', '请输入用户名。')
                    : null,
              ),
              const SizedBox(height: 12),
              TextFormField(
                key: const ValueKey('password'),
                controller: password,
                enabled: !widget.busy,
                obscureText: true,
                autocorrect: false,
                enableSuggestions: false,
                decoration: InputDecoration(labelText: t('Password', '密码')),
                validator: (value) => (value ?? '').isEmpty
                    ? t('Enter your password.', '请输入密码。')
                    : null,
              ),
              if (widget.error case final error?) ...[
                const SizedBox(height: 10),
                Text(errorLabel(context, error)),
              ],
              const SizedBox(height: 16),
              FilledButton(
                key: const ValueKey('sign-in'),
                onPressed: widget.busy ? null : _submit,
                child: Text(
                  widget.busy ? t('Signing in…', '正在登录…') : t('Sign in', '登录'),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _AccountStatus extends StatelessWidget {
  const _AccountStatus({
    required this.snapshot,
    required this.busy,
    required this.error,
  });

  final MobileSnapshot snapshot;
  final bool busy;
  final MobileErrorCode? error;

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      if (busy) const LinearProgressIndicator(),
      Text(snapshot.ownerLabel, style: Theme.of(context).textTheme.titleLarge),
      Text(syncLabel(context, snapshot.syncStatus)),
      if (snapshot.pendingCount > 0)
        Text(
          uiText(
            context,
            'Pending changes ${snapshot.pendingCount}',
            '待同步改动 ${snapshot.pendingCount}',
          ),
        ),
      if (error case final problem?) Text(errorLabel(context, problem)),
    ],
  );
}

class _Entries extends StatelessWidget {
  const _Entries({
    required this.ledger,
    required this.records,
    required this.locked,
    required this.edit,
    required this.delete,
  });

  final MobileLedger ledger;
  final List<MobileEntry> records;
  final bool locked;
  final ValueChanged<MobileEntry> edit;
  final ValueChanged<MobileEntry> delete;

  @override
  Widget build(BuildContext context) {
    String t(String english, String chinese) =>
        uiText(context, english, chinese);
    final rows = [...records]
      ..sort((left, right) {
        final byDate = right.date.compareTo(left.date);
        return byDate == 0 ? left.id.compareTo(right.id) : byDate;
      });
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (ledger.archived) Text(t('Archived ledger', '已归档账本')),
        if (rows.isEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 14),
            child: Text(t('No entries in this ledger yet.', '此账本暂无记录。')),
          ),
        for (final record in rows) _entryCard(context, record),
      ],
    );
  }

  Widget _entryCard(BuildContext context, MobileEntry entry) {
    String t(String english, String chinese) =>
        uiText(context, english, chinese);
    final entryLocked = locked || ledger.archived;
    return Card(
      key: ValueKey('entry-${entry.id}'),
      margin: const EdgeInsets.only(top: 12),
      child: ExpansionTile(
        title: Text('${entry.draft.amount} ${entry.draft.assetId}'),
        subtitle: Text(
          '${entry.draft.date} · ${kindLabel(context, entry.draft.kind)}',
        ),
        childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
        expandedCrossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _References(ledger: ledger, draft: entry.draft),
          if (entry.draft.description.isNotEmpty) Text(entry.draft.description),
          Text(switch (entry.lifecycle) {
            EntryLifecycle.queued => t('Saved on this device', '已保存到设备'),
            EntryLifecycle.posted => t('Posted', '已入账'),
            EntryLifecycle.conflict => t('Conflict to review', '待核对冲突'),
          }),
          Wrap(
            spacing: 8,
            children: [
              if (entry.canEdit)
                TextButton(
                  onPressed: entryLocked ? null : () => edit(entry),
                  child: Text(t('Edit entry', '编辑记录')),
                ),
              if (entry.canDelete)
                TextButton(
                  onPressed: entryLocked ? null : () => delete(entry),
                  child: Text(
                    entry.lifecycle == EntryLifecycle.posted
                        ? t('Cancel entry', '取消记录')
                        : t('Delete draft', '删除草稿'),
                  ),
                ),
            ],
          ),
        ],
      ),
    );
  }
}

class _SyncView extends StatelessWidget {
  const _SyncView({
    required this.snapshot,
    required this.busy,
    required this.sync,
    required this.resolve,
  });

  final MobileSnapshot snapshot;
  final bool busy;
  final VoidCallback sync;
  final void Function(MobileConflict, ConflictChoice) resolve;

  @override
  Widget build(BuildContext context) {
    String t(String english, String chinese) =>
        uiText(context, english, chinese);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        FilledButton.icon(
          key: const ValueKey('sync-now'),
          onPressed: busy ? null : sync,
          icon: const Icon(Icons.sync),
          label: Text(t('Sync now', '立即同步')),
        ),
        if (snapshot.lastSyncedAt case final time?)
          Text('${t('Last synced at', '上次同步时间')} $time'),
        const SizedBox(height: 16),
        Text(
          t('Conflicts', '冲突'),
          style: Theme.of(context).textTheme.titleLarge,
        ),
        if (snapshot.conflicts.isEmpty)
          Text(t('No conflicts to review.', '暂无待核对冲突。')),
        for (final conflict in snapshot.conflicts)
          Card(
            key: ValueKey('conflict-${conflict.id}'),
            child: ExpansionTile(
              title: Text(
                snapshot.ledgers
                        .where((item) => item.id == conflict.ledgerId)
                        .firstOrNull
                        ?.name ??
                    conflict.ledgerId,
              ),
              subtitle: Text(t('Compare the two versions', '比较两个版本')),
              childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
              expandedCrossAxisAlignment: CrossAxisAlignment.start,
              children: [
                _Version(
                  title: t('Local submission', '本地提交'),
                  draft: conflict.submitted,
                  version: conflict.submittedVersion,
                  ledgerId: conflict.ledgerId,
                  ledgers: snapshot.ledgers,
                ),
                _Version(
                  title: conflict.latestVersion == null
                      ? t('Current server version', '当前服务器版本')
                      : t('Server version at the conflict', '发生冲突时的服务器版本'),
                  draft: conflict.current,
                  version: conflict.currentVersion,
                  ledgerId: conflict.ledgerId,
                  ledgers: snapshot.ledgers,
                  deleted: conflict.serverDeleted,
                ),
                if (conflict.latestVersion != null)
                  _Version(
                    title: t('Latest server version', '最新服务器版本'),
                    draft: conflict.latest,
                    version: conflict.latestVersion,
                    ledgerId: conflict.ledgerId,
                    ledgers: snapshot.ledgers,
                    deleted: conflict.latestDeleted,
                  ),
                Wrap(
                  spacing: 8,
                  children: [
                    OutlinedButton(
                      onPressed: busy
                          ? null
                          : () => resolve(conflict, ConflictChoice.keepCurrent),
                      child: Text(t('Keep server version', '保留服务器版本')),
                    ),
                    if (_canSubmitLocal(conflict))
                      FilledButton(
                        onPressed: busy
                            ? null
                            : () =>
                                  resolve(conflict, ConflictChoice.submitLocal),
                        child: Text(
                          t(
                            'Submit local changes as a new revision',
                            '将本地改动提交为新修订',
                          ),
                        ),
                      ),
                  ],
                ),
              ],
            ),
          ),
      ],
    );
  }
}

class _Version extends StatelessWidget {
  const _Version({
    required this.title,
    required this.draft,
    required this.version,
    required this.ledgerId,
    required this.ledgers,
    this.deleted = false,
  });

  final String title;
  final MobileDraft? draft;
  final int? version;
  final String ledgerId;
  final List<MobileLedger> ledgers;
  final bool deleted;

  @override
  Widget build(BuildContext context) {
    final value = draft?.ledgerId == ledgerId ? draft : null;
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(title, style: Theme.of(context).textTheme.titleSmall),
          if (version != null)
            Text('${uiText(context, 'Version', '版本')} $version'),
          if (deleted)
            Text(uiText(context, 'Deleted on the server', '已在服务器删除'))
          else if (value == null)
            Text(uiText(context, 'Entry details unavailable', '暂无记录明细'))
          else ...[
            Text(
              '${value.amount} ${value.assetId} · ${value.date} · ${kindLabel(context, value.kind)}',
            ),
            _References(
              ledger: ledgers.where((item) => item.id == ledgerId).firstOrNull,
              draft: value,
            ),
            if (value.description.isNotEmpty) Text(value.description),
          ],
        ],
      ),
    );
  }
}

class _References extends StatelessWidget {
  const _References({required this.ledger, required this.draft});

  final MobileLedger? ledger;
  final MobileDraft draft;

  @override
  Widget build(BuildContext context) {
    final account =
        ledger?.accounts
            .where((item) => item.id == draft.accountId)
            .firstOrNull
            ?.name ??
        draft.accountId;
    final category =
        ledger?.categories
            .where((item) => item.id == draft.categoryId)
            .firstOrNull
            ?.name ??
        draft.categoryId;
    return Text(
      '${uiText(context, 'Account', '账户')} $account · '
      '${uiText(context, 'Category', '分类')} $category',
    );
  }
}
