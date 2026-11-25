import 'package:material_ui/material_ui.dart';

import 'mobile_adapter.dart';

String uiText(BuildContext context, String english, String chinese) =>
    Localizations.localeOf(context).languageCode == 'zh' ? chinese : english;

String kindLabel(BuildContext context, EntryKind kind) => switch (kind) {
  EntryKind.income => uiText(context, 'Income', '收入'),
  EntryKind.expense => uiText(context, 'Expense', '支出'),
};

String syncLabel(BuildContext context, SyncStatus status) => switch (status) {
  SyncStatus.ready => uiText(context, 'Ready to sync', '等待同步'),
  SyncStatus.offline => uiText(context, 'Working offline', '离线使用中'),
  SyncStatus.pending => uiText(context, 'Pending sync', '待同步'),
  SyncStatus.synced => uiText(context, 'Synced', '已同步'),
  SyncStatus.signInRequired => uiText(context, 'Sign in to sync', '登录后同步'),
  SyncStatus.reviewRequired => uiText(
    context,
    'Sync paused. Check the server recovery or device status. Your drafts and receipts are still on this device.',
    '同步已暂停。请核对服务器恢复记录或设备状态，草稿和票据原件仍保存在此设备。',
  ),
};

String errorLabel(BuildContext context, MobileErrorCode code) => switch (code) {
  MobileErrorCode.connection => uiText(
    context,
    'Could not connect. Check the server and your connection.',
    '无法连接，请检查服务器和网络。',
  ),
  MobileErrorCode.signIn => uiText(
    context,
    'Sign-in failed. Check your details and try again.',
    '登录失败，请核对登录信息后重试。',
  ),
  MobileErrorCode.serverAddress => uiText(
    context,
    'Enter a valid server address.',
    '请输入有效的服务器地址。',
  ),
  MobileErrorCode.operation => uiText(
    context,
    'Could not save this change.',
    '无法保存这项改动。',
  ),
  MobileErrorCode.photo => uiText(
    context,
    'Could not save the receipt photo.',
    '无法保存票据照片。',
  ),
  MobileErrorCode.photoRecovery => uiText(
    context,
    'Could not recover the receipt photo. Open the intended entry to retake it.',
    '无法恢复票据照片。请打开对应的记录重新拍摄。',
  ),
  MobileErrorCode.photoRelease => uiText(
    context,
    'Could not release this receipt upload. Refresh the entry and try again.',
    '无法释放此票据上传。请刷新记录后重试。',
  ),
  MobileErrorCode.sync => uiText(
    context,
    'Sync interrupted. Saved changes can be synced again.',
    '同步中断，已保存的改动可以再次同步。',
  ),
  MobileErrorCode.conflict => uiText(
    context,
    'Could not record the conflict decision. Review the two versions.',
    '无法记录冲突处理结果，请核对两个版本。',
  ),
};

String formatDate(DateTime date) =>
    '${date.year.toString().padLeft(4, '0')}-'
    '${date.month.toString().padLeft(2, '0')}-'
    '${date.day.toString().padLeft(2, '0')}';

class LanguageButton extends StatelessWidget {
  const LanguageButton({required this.onPressed, super.key});

  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) => TextButton(
    key: const ValueKey('app-language'),
    onPressed: onPressed,
    child: Text(uiText(context, '中文', 'English')),
  );
}
