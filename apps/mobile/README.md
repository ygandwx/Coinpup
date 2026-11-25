# Coinpup 移动端

Flutter 项目包含 Android 与 iOS 平台工程。移动端规则见 [AGENTS.md](AGENTS.md)。

先在联网时登录并同步账本、账户、分类和价格。移动端支持离线增加、修改和删除简单收支、浏览已同步的收支及拍照。缓存的期初、转账和换汇也可离线查看，保留原币数量、转出和转入双方以及单列的手续费。已入账记录的更正和取消保留服务端历史。期初、转账、换汇的更正和取消，以及含手续费、多分摊或不同归属日期的收支更正，可在网页操作。

本地数据库按服务器与所有者隔离。同步命令在发送前持久化完整请求和幂等键。结果未知时原样重放请求，然后推进已核对的通知游标。已取消记录保留终止标记，冲突显示两边版本并等待明确处理。拍照后先复制到私有目录，联网后使用原上传 ID 从确认的服务端偏移继续。报价区分缺价和已有值，显示来源、观察时间及缓存时间。

原生会话固定在登录的服务器及所有者，续用服务端可撤销会话、精确来源和 CSRF 检查。设备 UUID 用于同步证据。会话令牌保存在平台安全存储，首次联网和恢复连接都核对管理员身份。连接中断时可继续使用本地缓存，服务端明确拒绝会话时隐藏工作区，待登录后可以继续处理原草稿。

使用 Flutter 3.50.0 和 Dart 3.14 运行开发检查。

```sh
flutter pub get
flutter analyze
flutter test
```

真实协议测试需要仅绑定本机的独立实例。实例应使用 PostgreSQL 17、同步及断点上传 API、两个虚构账本和测试日的手工报价。通过测试进程环境提供 `COINPUP_MOBILE_TEST_URL`、`COINPUP_MOBILE_TEST_USERNAME` 和 `COINPUP_MOBILE_TEST_PASSWORD`。带这三项配置的 `flutter test test/sync_runtime_test.dart` 会验证双设备冲突、断网重启、丢失回复后的精确重放、断点续传和取消终态。

可为具备合成报价或附件释放 API 的独立实例附加 `COINPUP_MOBILE_TEST_EXPECT_AUTOMATIC=1` 或 `COINPUP_MOBILE_TEST_EXPECT_RELEASE=1`。后者验证暂停在已存分块后，放弃回执丢失也能在重启后重放，已完成文件保持可用。

Android 本地构建使用 JDK 17、已授权的 Android SDK 和 Flutter 配套构建工具。使用 `flutter build apk --debug` 构建测试 APK。通过 `adb reverse` 将设备回环连接到本机测试端口，调试 APK 允许回环 HTTP。正式配置使用 HTTPS。iOS 编译在具备 Xcode 的 macOS 环境执行 `flutter build ios --no-codesign`。
