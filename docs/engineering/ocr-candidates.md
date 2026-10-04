# OCR 候选隔离环境

本环境只验证依赖、原生加载和离线初始化，不识别票据，也不代表 [ADR 0019](../architecture/decisions/0019-local-ocr-worker.md) 达标。正式选型遵守[冻结协议](ocr-benchmark.md)，进度只见 [status.md](status.md)。构建入口是独立 [Dockerfile](../../ops/ocr-benchmark/Dockerfile)，默认 API 镜像和四份原有依赖锁保持独立。

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
