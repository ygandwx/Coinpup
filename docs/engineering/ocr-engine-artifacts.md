# 固定 OCR 候选制品

本工具落实 [同机协议](ocr-benchmark.md) 的构建期取得、离线核验要求。唯一清单为 [engine-assets.json](../../tests/fixtures/ocr/engine-assets.json)，工具为 [engine_assets.py](../../scripts/ocr_benchmark/engine_assets.py)。进度只见 [status.md](status.md)；真实构建、加载、识别和资源结果必须另有实测证据。

## 固定来源与边界

清单面向 CPython 3.12、Linux x86_64、CPU：固定 PaddleOCR/PaddleX 3.7.0、PaddlePaddle 3.4.0 CPU wheel、OpenCV contrib 4.10.0.84；PP-OCRv6 small 检测/识别目录绑定不可变模型提交。Tesseract 5.5.3 源码及 tessdata fast/best 4.1.0 语言数据均绑定实际提交。语言候选为简中、繁中和英文；最终配置只能在独立开发样本上决定。

每个可直取文件都有固定 HTTPS URL、实际字节数、SHA-256 和来源角色；源码/模型说明、GNU Runtime Exception 及第三方通知保留完整原文。大文件和缓存不提交仓库。来自 vendor archive 的嵌入通知注明 archive 摘要与 member，不能冒充可单独下载的源文件。

```sh
python -m scripts.ocr_benchmark.engine_assets fetch --output-dir build/engine-assets
python -m scripts.ocr_benchmark.engine_assets verify --output-dir build/engine-assets
COINPUP_ENGINE_ASSETS=build/engine-assets python scripts/check.py assets
```

只有显式 fetch 联网；命中完整缓存后不重新下载。摘要或尺寸不符、链接目录、非法路径、超预算、重定向到未列出的来源均失败，不覆盖坏缓存。verify 与真实 ZIP 检查不安装、不 import、不初始化引擎；普通单元测试只用虚构小文件与模拟网络。真实 profile 缺少明确缓存会失败，不以 skip 当作验收。

## 许可证与后续构建

模型卡声明 Apache-2.0，但固定目录没有独立 LICENSE；按事实记录，不能补造许可证文件。Paddle wheel 内的 18 个原生库另有实际摘要；其完整 vendor 通知保留独立角色，不能将整个 wheel 一概标为 Apache。GFortran/quadmath 的 GNU 许可证和 Runtime Exception 原文均保留；二进制编译器补丁版本尚无法从现有信息确定。

OpenCV 第三方通知中的 Affero 提及属于 macOS libsrt 的 MPL 次级许可证定义，Linux wheel 不含该库；GNU GPL 第 13 条的 AGPL 兼容条款也不代表该组件改用 AGPL。判断须绑定这个固定文件、平台与适用组件，不能删除原文、通用放行 AGPL 或弱化已有依赖审计。

此步骤证明文件完整性、真实 wheel metadata、原生库清单及原始通知存在。它不证明完整传递依赖已解析、原生动态链接已成功、Tesseract 已编译或所有活跃依赖许可证已核验。隔离环境构建还须锁定全部传递依赖、实际安装包和编译工具链，核对活跃许可链并实际加载；默认 API 的锁、镜像和启动路径不引入重型 OCR 库。运行期禁止网络与缺模型自动下载，正式门槛始终按 ADR 0019。
