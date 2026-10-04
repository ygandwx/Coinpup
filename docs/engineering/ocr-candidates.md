# OCR 候选隔离环境

本环境提供依赖/原生加载审计、离线初始化及独立开发样本识别；这些步骤不代表 [ADR 0019](../architecture/decisions/0019-local-ocr-worker.md) 达标。正式选型遵守[冻结协议](ocr-benchmark.md)，进度只见 [status.md](status.md)。构建入口是独立 [Dockerfile](../../ops/ocr-benchmark/Dockerfile)，默认 API 镜像和四份原有依赖锁保持独立。

## 构建与来源

[固定制品](ocr-engine-artifacts.md)保存模型、语言包和原始通知；[构建输入](../../ops/ocr-benchmark/build-inputs.json)绑定 Python 3.12.14 amd64 OCI 摘要、Debian 签名快照、直接系统包版本/摘要，以及 Tesseract 5.5.3、Leptonica 1.86 源码。Python 原始通知从与 OCI 构建摘要相同的官方源码包按冻结成员逐字节提取，已知嵌入组件另绑定完整原通知。快照按固定日期重放，签名仍校验；安装事实另保存所有实际包、版权原文、源码许可、CMake 参数和二进制摘要。

[Paddle 输入](../../ops/ocr-benchmark/paddle.in)及[完整锁](../../ops/ocr-benchmark/paddle.lock)使用原 OCR 版本约束，实际 wheel 来源和全部通知见[依赖来源](../../ops/ocr-benchmark/paddle-provenance.json)。构建先获取固定来源及哈希 wheelhouse，再在断网阶段离线安装和编译。不能靠跳过依赖、替换 headless 包或升级默认 API 环境解决候选问题。

Tesseract 和 Paddle 使用独立 venv。Tesseract 仅保留所需 PNG/JPEG/Zlib 支持，关闭网络下载、训练、测试、旧引擎、图形及 OpenMP；Paddle 使用本地 small 检测/识别模型，关闭额外方向分类、展开及 HPI，显式单线程。两个目标均使用固定 bootstrap。候选镜像不包含正式真值与评分答案。

准确构建和运行命令见 [CI](../../.github/workflows/ci.yml) 的候选 job。运行时只读、非 root、无网络、无 capabilities、禁止新增权限；绑定同两个可用逻辑 CPU、4 GiB、无 swap、pids 128、单线程，私有有界 tmpfs 保存临时缓存，唯一结果目录可写。

## 实际审计与失败

[engine_environment.py](../../scripts/ocr_benchmark/engine_environment.py)先核验固定模型字节，再校验 pip、实际安装 metadata、完整通知和来源政策。初始化只加载本地模型，不读取账单；缺模型在构造前返回稳定错误。关闭模型来源探测不能代替容器禁网。

初始化后收集实际映射的 ELF 和 ldd 所需闭包，按[原生 wheel 政策](../../ops/ocr-benchmark/native-wheels-policy.json)与[系统／源码政策](../../ops/ocr-benchmark/native-system-policy.json)逐个匹配原字节 SHA、大小、适用许可及证据。政策证据须绑定实际通知原文或已冻结来源，不能只凭包的 License 字段、删除通知、通用忽略 AGPL 字样或将未活跃库当作已运行组件。未审阅记录、缺通知、身份变化、缺动态库均明确失败。

独立 `ldd` 可能丢失加载它的父模块搜索上下文。审计子进程只补入实际映射的 ELF 目录，保存原检查输出和确切搜索路径，SDK 进程环境不变；解析到的每个依赖仍须核验实际文件及许可。Tesseract 逐套读取实际已加载语言列表，不能以初始化返回成功代替三个显式模型均已加载的证明；原 `best` 配置探测可选竖排子模型产生的警告保留在运行证据中。

失败仍保留实际 inventory、版权原文和初始化日志供补核证据，状态保持 failed；不得将空政策或来源声明算作通过。原生来源的未知信息如实保存。报告只涉及公开制品和虚构环境，不存票据、账号、连接信息或开发机路径。

```sh
COINPUP_ENGINE_ASSETS=build/engine-assets COINPUP_CANDIDATE_REPORTS=build/candidate-reports python scripts/check.py candidates
```

真实 profile 校验由上述容器实际生成的报告，不在宿主机重新加载引擎；没有明确报告目录或缺项时失败，不 skip。普通测试使用虚构记录和 mock，不在 API 环境安装重型依赖。成功初始化仍须完成独立开发配置、冻结正式输入与同机质量／资源验收，才能接入生产 worker。

## 真实识别与开发配置

[适配器](../../services/api/src/coinpup_api/ocr/engine_adapters.py)只接收 RGB 字节及真实尺寸，返回 SDK 实际整行文字/框；Tesseract 使用 C API，Paddle 使用本地 small 的 BGR 数组入口。固定 CPU 单线程、FP32，关闭 HPI、MKLDNN、CINN、TensorRT 及辅助方向/展开模型；检测上限与分数参数均写入实际配置元数据。保留内部空格和标点，不推造细框或补字段。

[共享准备与识别](../../services/api/src/coinpup_api/ocr/recognize.py)在原 bitmap/image 上下文内转换 RGB 并计算摘要，消费结束后释放资源；文字层使用真实几何和原词框，已存在的坏层只进人工路径。每页保留物理序号，坏页不能把后页移位。纯准备默认路径和响应保持原样，回调仅供内部识别使用。

[开发运行器](../../scripts/ocr_benchmark/development_run.py)在宿主评分，每个容器只绑定经核验的模型与随机名称私有原件，不绑定真值/模板/来源映射。固定 profile 的持久 [NDJSON 进程](../../scripts/ocr_benchmark/candidate_session.py)每次读取有界请求并释放当页数据；启动 120 秒、每页 60 秒，只有唯一真实页完成才能推进期限，输出/诊断均有限额。容器删除与本地进程回收未确认时失败，不能继续下一候选。

```sh
python -m scripts.ocr_benchmark.development_run --corpus-dir build/fictional-corpus --assets-dir build/engine-assets --output-dir build/development-reports
```

输出目录必须不存在；仅新建的虚构原件 staging 子树和该组公开制品审计目录交给容器用户。每组保留两次完整实际响应、真实 raster 摘要、单调纳秒时间与开发选择。原件准备及批量文字提取计入首个页面期限；完成次序可不同于物理页序，结果按实际物理序号保存。

进程在 bitmap/image 仍存活时收集实际原生映射；显式 `finish` 后才做完整加载/动态依赖审计并释放模型。它复用同一环境初始化报告中的实际原通知证据，重新核验现行来源/政策摘要与识别后文件的原字节，缺库、未知来源或通知不匹配必须失败。父进程须取得审计通过及报告摘要、确认正常退出和容器删除后才允许开发选型；初始化报告不能授权跳过识别后新增库。

准备时间是总处理墙钟减识别与解析后的残差，包含协调开销；此步骤没有正式 RSS/冷热性能测量，不能当作正式对比数据。非默认 CropBox 的文字页在识别路径保守交给人工，不改变纯准备的既有响应，也不能转去 OCR。
