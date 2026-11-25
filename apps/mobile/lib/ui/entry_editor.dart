import 'package:material_ui/material_ui.dart';

import '../exact_amount.dart';
import 'mobile_adapter.dart';
import 'mobile_text.dart';

class EntryEditor extends StatefulWidget {
  const EntryEditor({
    required this.ledger,
    required this.assets,
    required this.save,
    required this.changeLanguage,
    this.previous,
    super.key,
  });

  final MobileLedger ledger;
  final List<MobileAsset> assets;
  final MobileEntry? previous;
  final Future<bool> Function(MobileDraft) save;
  final VoidCallback changeLanguage;

  @override
  State<EntryEditor> createState() => _EntryEditorState();
}

class _EntryEditorState extends State<EntryEditor> {
  final form = GlobalKey<FormState>();
  late final TextEditingController amount;
  late final TextEditingController date;
  late final TextEditingController description;
  late EntryKind kind;
  String? accountId;
  String? assetId;
  String? categoryId;
  bool saving = false;
  bool failed = false;

  @override
  void initState() {
    super.initState();
    final previous = widget.previous?.draft;
    amount = TextEditingController(text: previous?.amount ?? '');
    date = TextEditingController(
      text: previous?.date ?? formatDate(DateTime.now()),
    );
    description = TextEditingController(text: previous?.description ?? '');
    kind = previous?.kind ?? EntryKind.expense;
    accountId = previous?.accountId;
    assetId = previous?.assetId;
    categoryId = previous?.categoryId;
    _align();
  }

  @override
  void dispose() {
    amount.dispose();
    date.dispose();
    description.dispose();
    super.dispose();
  }

  List<MobileCategory> get categories =>
      widget.ledger.categories.where((value) => value.kind == kind).toList();

  List<MobileAsset> get assets {
    final accounts = widget.ledger.accounts
        .where((value) => value.id == accountId)
        .expand((value) => value.assetIds)
        .toSet();
    return widget.assets.where((value) => accounts.contains(value.id)).toList();
  }

  MobileAsset? get asset =>
      assets.where((value) => value.id == assetId).firstOrNull;

  void _align() {
    accountId = _selection(accountId, widget.ledger.accounts.map((a) => a.id));
    assetId = _selection(assetId, assets.map((a) => a.id));
    categoryId = _selection(categoryId, categories.map((c) => c.id));
  }

  String? _selection(String? selected, Iterable<String> choices) =>
      choices.contains(selected) ? selected : choices.firstOrNull;

  String? _checkAmount(String? value) {
    final selected = asset;
    if (selected != null) {
      try {
        final exact = ExactAmount.parse(
          value ?? '',
          selected.id,
          selected.scale,
        );
        if (exact.units > BigInt.zero) return null;
      } on FormatException {
        // Keep the exact input available for correction.
      }
    }
    return uiText(
      context,
      'Enter a positive decimal in the asset precision.',
      '请输入符合资产精度的正数。',
    );
  }

  String? _checkDate(String? value) {
    final parsed = DateTime.tryParse(value ?? '');
    if (parsed != null && parsed.year > 0 && formatDate(parsed) == value) {
      return null;
    }
    return uiText(context, 'Enter a valid YYYY-MM-DD date.', '请输入有效的年月日。');
  }

  Future<void> _submit() async {
    if (saving || !form.currentState!.validate()) return;
    final confirmedAccount = accountId;
    final confirmedCategory = categoryId;
    final selected = asset;
    if (confirmedAccount == null ||
        confirmedCategory == null ||
        selected == null) {
      return;
    }
    setState(() {
      saving = true;
      failed = false;
    });
    final saved = await widget.save(
      MobileDraft(
        ledgerId: widget.ledger.id,
        kind: kind,
        accountId: confirmedAccount,
        categoryId: confirmedCategory,
        assetId: selected.id,
        amount: amount.text,
        date: date.text,
        description: description.text,
      ),
    );
    if (!mounted) return;
    if (saved) {
      Navigator.pop(context);
    } else {
      setState(() {
        saving = false;
        failed = true;
      });
    }
  }

  Widget _choice(
    String key,
    String label,
    String? current,
    Iterable<(String, String)> options,
    ValueChanged<String> change, {
    bool enabled = true,
  }) {
    return InputDecorator(
      decoration: InputDecoration(labelText: label),
      child: DropdownButtonHideUnderline(
        child: DropdownButton<String>(
          key: ValueKey(key),
          value: current,
          isExpanded: true,
          isDense: true,
          items: [
            for (final (value, title) in options)
              DropdownMenuItem(
                value: value,
                child: Text(title, overflow: TextOverflow.ellipsis),
              ),
          ],
          onChanged: saving || !enabled
              ? null
              : (value) {
                  if (value == null) return;
                  setState(() {
                    change(value);
                    _align();
                  });
                },
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    String t(String english, String chinese) =>
        uiText(context, english, chinese);
    final previous = widget.previous;
    return PopScope(
      canPop: !saving,
      child: Scaffold(
        appBar: AppBar(
          title: Text(
            previous == null ? t('New entry', '新增记录') : t('Edit entry', '编辑记录'),
          ),
          actions: [LanguageButton(onPressed: widget.changeLanguage)],
        ),
        body: SafeArea(
          child: Form(
            key: form,
            child: ListView(
              key: const ValueKey('entry-form-scroll'),
              padding: const EdgeInsets.all(16),
              children: [
                Text(
                  widget.ledger.name,
                  style: Theme.of(context).textTheme.titleMedium,
                ),
                if (previous?.lifecycle == EntryLifecycle.posted)
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Text(
                      t('Saving makes a correcting entry.', '保存后将以更正分录入账。'),
                    ),
                  ),
                const SizedBox(height: 16),
                _choice(
                  'entry-kind',
                  t('Type', '类型'),
                  kind.name,
                  EntryKind.values.map(
                    (value) => (value.name, kindLabel(context, value)),
                  ),
                  (value) => kind = EntryKind.values.byName(value),
                  enabled: previous?.lifecycle != EntryLifecycle.posted,
                ),
                const SizedBox(height: 12),
                _choice(
                  'entry-account',
                  t('Account', '账户'),
                  accountId,
                  widget.ledger.accounts.map((value) => (value.id, value.name)),
                  (value) => accountId = value,
                ),
                const SizedBox(height: 12),
                _choice(
                  'entry-asset',
                  t('Asset', '资产'),
                  assetId,
                  assets.map((value) => (value.id, value.id)),
                  (value) => assetId = value,
                ),
                const SizedBox(height: 12),
                _choice(
                  'entry-category',
                  t('Category', '分类'),
                  categoryId,
                  categories.map((value) => (value.id, value.name)),
                  (value) => categoryId = value,
                ),
                const SizedBox(height: 12),
                TextFormField(
                  key: const ValueKey('entry-amount'),
                  enabled: !saving,
                  controller: amount,
                  keyboardType: const TextInputType.numberWithOptions(
                    decimal: true,
                  ),
                  decoration: InputDecoration(labelText: t('Amount', '金额')),
                  validator: _checkAmount,
                ),
                const SizedBox(height: 12),
                TextFormField(
                  key: const ValueKey('entry-date'),
                  enabled: !saving,
                  controller: date,
                  keyboardType: TextInputType.datetime,
                  decoration: InputDecoration(
                    labelText: t('Date', '日期'),
                    hintText: 'YYYY-MM-DD',
                  ),
                  validator: _checkDate,
                ),
                const SizedBox(height: 12),
                TextFormField(
                  key: const ValueKey('entry-description'),
                  enabled: !saving,
                  controller: description,
                  minLines: 1,
                  maxLines: 3,
                  maxLength: 2000,
                  decoration: InputDecoration(
                    labelText: t('Description', '说明'),
                  ),
                ),
                if (failed)
                  Text(
                    errorLabel(context, MobileErrorCode.operation),
                    semanticsLabel: t('Save failed', '保存失败'),
                  ),
                const SizedBox(height: 12),
                FilledButton(
                  key: const ValueKey('save-entry'),
                  onPressed: saving || asset == null || categoryId == null
                      ? null
                      : _submit,
                  child: Text(
                    saving
                        ? t('Saving…', '正在保存…')
                        : t('Save on device', '保存到设备'),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
