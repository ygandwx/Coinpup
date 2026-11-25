# Coinpup 移动端

Flutter 项目包含 Android 与 iOS 工程。移动端规则见 [AGENTS.md](AGENTS.md)。

首次联网登录并同步账本、账户与分类。移动端可离线新增、修改和删除简单收支草稿，查看已同步的收支，并提交更正或取消。已入账记录的旧版本由服务端保留。期初、转账、换汇及其他经营流水可在网页查看。

本地数据库按服务器和所有者隔离。客户端先保存完整命令、原版本与幂等键，再发送。网络结果未知时重放同一请求。取消记录保留终止标记，版本冲突保留双方证据，等待用户明确处理。服务端拒绝会话时隐藏缓存，重新登录原账户后可以继续处理草稿。

使用 Flutter 3.50.0 和 Dart 3.14 运行检查。

```sh
flutter pub get
flutter analyze
flutter test
```

真实同步测试使用仅绑定本机的独立 Coinpup 和 PostgreSQL 17，预先创建两个虚构账本。通过环境变量提供 `COINPUP_MOBILE_TEST_URL`、`COINPUP_MOBILE_TEST_USERNAME` 和 `COINPUP_MOBILE_TEST_PASSWORD`。运行 `flutter test test/sync_runtime_test.dart` 验证断网重启、回执重放、双设备冲突与取消终态。

Android 构建使用 JDK 17 及已授权的 Android SDK。测试实例可通过 `adb reverse` 转发设备回环端口。调试构建允许回环 HTTP，正式使用 HTTPS。iOS 编译在具备 Xcode 的 macOS 环境运行 `flutter build ios --no-codesign`。
