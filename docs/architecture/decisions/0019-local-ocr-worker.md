# ADR 0019：本地文字提取、OCR 与隔离 worker

日期：2026-10-04。状态：草稿，待用户确认；本文件不批准实现。

## 背景

PDF 文字层、扫描账单和发票照片需要中文、英文识别。原件可能无法解析，识别结果可能错误；上传和识别都不产生费用。遵循优化计划第 5 节：共用 Python 包、独立 worker、PostgreSQL 队列、本地离线、人工确认后调用财务命令。

## 决定

待确认推荐：先提取可靠文字层；无文字层或质量不足的页面才渲染并 OCR。PDF 以 pdfplumber + pypdfium2 为基线；PaddleOCR small CPU 与 Tesseract 同机验收后决定默认引擎。模型与依赖固定版本、许可证清单和摘要，禁止运行时联网下载。

| 候选及调研版本 | 中英文能力与边界 | CPU/内存与部署 | 许可证 |
| --- | --- | --- | --- |
| pypdf 6.19.0 | 已有文字层；无 OCR，乱码编码需转人工/OCR | 纯 Python，较轻；异常巨型内容流仍可耗尽内存，须限制流大小 | BSD-3-Clause |
| pdfplumber 0.11.10 | pdfminer.six 提取字符位置/表格；无 OCR，扫描页另处理 | 字符对象与页面缓存增大内存，逐页关闭；依赖 Pillow、pdfminer.six 等，离线 wheel 安装 | MIT |
| pypdfium2 5.13.0 | 提取文字及渲染，供 OCR 输入 | 原生 PDFium wheel；非线程安全，独立进程逐页释放；分辨率影响 CPU/RSS | Python 桥 Apache-2.0/BSD-3-Clause；PDFium BSD 及随附第三方声明 |
| PyMuPDF 1.28.2 | 文字/版面/渲染；其 OCR 仍需 Tesseract 与语言包 | 原生库，部署较简便；不把旧厂商速度比较当本服务器指标 | AGPL-3.0 或商业许可，先确认分发与许可路径 |
| Tesseract 5.5.3 | eng/chi_sim/chi_tra；清晰印刷中英文，旋转/复杂表格另处理 | C++/Leptonica CLI；fast 与 best 模型速度/质量不同，离线预装语言包；模型磁盘大小不等于 RSS | Apache-2.0，语言数据也须核对对应许可证 |
| PaddleOCR 3.7.0 / PP-OCRv6 | 中文、英文及混排检测识别；表格结构另评估，不直接等同字段正确率 | Python/Paddle CPU 原生依赖与模型部署较重；tiny/small/medium 权重规模 1.5M/7.7M/34.5M 参数，不是内存 MB | Apache-2.0，模型与传递依赖逐项登记 |

公开资料没有可直接比较上述候选的同机账单峰值 RSS。PP-OCRv6 的厂商 200 图、Xeon 8350C 测试报告 PaddlePaddle tiny/small/medium 为 0.32/0.79/2.05 秒/图；线程、输入与运行后端影响结果，不能据此承诺本项目延迟。候选必须用相同虚构样本、DPI、线程数与机器实测冷启动、CPU 时间、峰值 RSS、p50/p95 与字段质量后定型。

- 模块 `coinpup_api/ocr/`，进程 `python -m coinpup_api.ocr.worker`；重依赖只在可选组 `ocr`，Docker `worker` target 安装，API 镜像不安装。PDFium/OCR 子进程设置时间、页数、像素、解压流、CPU/RSS 和并发预算，异常子进程可终止并回收。
- PostgreSQL `ocr_jobs` 唯一键绑定所有者、账本、原件、处理配置摘要与稳定 `intent_id`；原意图重试不重复创建，用户显式重识别生成新意图，不覆盖旧结果。状态 pending/running/succeeded/failed，记录尝试次数、租约到期、不可猜 token、稳定错误码和结果版本。领取、续租、失败重排与完成的每个短写事务均先取咨询锁，再按统一业务锁顺序取得必要的账本/主体及任务行锁，不能在 AFTER 触发器处才倒置取锁；`FOR UPDATE SKIP LOCKED` 领取并提交租约后，解析/计算在事务外，不能持有咨询锁或业务行锁等待 OCR。
- 完成事务先遵循咨询锁→账本→主体的顺序，再按 token、租约与原件归属验证；CAS 防过期 worker 写回，唯一约束防重复草稿。失败重试有界，永久格式错误转人工。任务/草稿是业务表，接入变更日志；领取等内部状态也明确同步策略，不能遗漏新增表。
- worker 仅只读私有原件，核对摘要/大小，无原件修改与公开下载端点；日志不打印全文、证件或票据信息。识别保存原始候选文字、字段、置信提示、页/坐标和引擎摘要；歧义金额/币种/日期标为需确认。归档原件或主体后的写回规则须在实现前确定并测试，不能新增账务绕过。
- 结果只生成带版本的草稿；用户可编辑字段及拆分分类，确认才以稳定幂等键调用现有财务命令。财务金额始终十进制字符串/整数最小单位，禁止二进制浮点；未知提交重放原意图。T05-3 处理任务和识别，T05-4 才接人工确认网页与入账。
- 任务和草稿与数据库进入 bundle；恢复时使用新 worker 实例及租约 token，过期 running 可重新领取，重复执行不能重复草稿/入账。原件按 ADR 0012 恢复；模型另保存摘要与离线制品。重任务队列不引入 Redis/Celery。

## 影响

确认后再按迁移/队列隔离、文字层与渲染、OCR 引擎、草稿读取/人工确认拆小 PR；新增鉴权接口、状态/限额配置和中英错误提示同步 OpenAPI。原有文件接口、回执、不可变分录保持；不得将本草稿或厂商数据作为已实现效果。

验收使用固定种子、标有“虚构测试票据”的 12 个模板（简中、繁中、英文与混排），每个生成文字 PDF、扫描 PDF、照片三种，共 36 份清晰样本，另 12 份旋转/低对比/模糊/透视样本及 8 类损坏、加密、超页数/像素/流大小、缺模型、超时等错误输入。独立人工编写真值清单，包含多币种、小数、负数、税额、合计、跨页与歧义日期；不从识别器输出反向制作真值。

拟议门槛（尚未测量）：清晰文字层样本金额/日期/币种精确率 100%；清晰 OCR 样本关键字段精确率至少 95%，同时报告 CER、表格行与金额恒等式，无法确定的字段必须显式待确认。退化样本评价错误发现与人工路径，禁止靠删除困难样本达标。同机对比 Tesseract fast/best 与 Paddle small；若门槛未达，反馈候选结果请用户决定，不自动降低标准。

安全与恢复验收覆盖两 worker 同时领取、崩溃/租约过期/旧 token 回写、重复提交、未知财务提交、跨账本隔离、断网启动、资源预算、坏文件、全 bundle 恢复与重试；所有测试只用上述虚构原件。

## 备选方案

用户需选择：是否接受推荐的文字层优先与 Paddle small/Tesseract 同机筛选，以及拟议字段门槛和人工兜底。若优先简化离线安装可先选 Tesseract；若希望使用 PyMuPDF，先选择满足 AGPL 的分发方式或商业许可。两种 OCR 都不预先宣称胜出。

依据（2026-10-04 核验）：[pypdf 文字提取与内存边界](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)、[pdfplumber](https://github.com/jsvine/pdfplumber/blob/stable/README.md)、[pypdfium2 5.13](https://github.com/pypdfium2-team/pypdfium2/blob/5.13.0/README.md)、[PyMuPDF 许可](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)、[Tesseract 语言模型](https://tesseract-ocr.github.io/tessdoc/Data-Files.html)、[PP-OCRv6 能力及基准](https://www.paddleocr.ai/main/en/version3.x/algorithm/PP-OCRv6/PP-OCRv6.html)、[PostgreSQL 17 SKIP LOCKED](https://www.postgresql.org/docs/17/sql-select.html#SQL-FOR-UPDATE-SHARE)。
