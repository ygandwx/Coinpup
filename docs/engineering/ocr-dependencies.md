# OCR 可选依赖与许可证清单

本文件对应 [ADR 0019](../architecture/decisions/0019-local-ocr-worker.md) 的 PDF 前置依赖。它不代表文字层路由、OCR 引擎、worker 部署或识别准确率已经实现；进度见 [status.md](status.md)，执行结果见对应 PR。

## 锁定与安装边界

`pyproject.toml` 的 `ocr` 可选组固定 pypdf、pdfplumber、pypdfium2；`requirements-ocr.lock` 包含其完整 Python 依赖和制品 SHA-256，并以 `requirements.lock` 约束既有 API 版本。基础 API 锁不因安装可选组而升级，不安装 PDF 库也必须能启动 API 和生成 OpenAPI。

只安装所需组，不启用库的 `full`、`crypto`、`image`、文档或测试 extras。pdfminer.six 本身依赖 cryptography；这是实际依赖链，不能因为未启用 pypdf 的 `crypto` extra 就把它漏记。实际依赖关系以已安装 wheel 的 `METADATA` 为准。

| 包 | 锁定版本 | 引入原因 | 包自身许可证与实际 wheel 文件 |
| --- | --- | --- | --- |
| pypdf | 6.19.0 | 有界内容流预检与文字操作符读取 | BSD-3-Clause；`pypdf-6.19.0.dist-info/licenses/LICENSE` |
| pdfplumber | 0.11.10 | 字符位置及文字提取 | MIT；`pdfplumber-0.11.10.dist-info/licenses/LICENSE.txt` |
| pypdfium2 | 5.13.0 | 原生 PDFium 渲染 | Apache-2.0 / BSD-3-Clause；另含文档/示例的 CC-BY-4.0 及原生第三方声明 |
| pdfminer.six | 20260107 | pdfplumber 的精确版本依赖 | MIT；`pdfminer_six-20260107.dist-info/licenses/LICENSE` |
| Pillow | 12.3.0 | pdfplumber 依赖及图像编码 | MIT-CMU；`pillow-12.3.0.dist-info/licenses/LICENSE` 同时包含原生组件通知 |
| charset-normalizer | 3.5.2 | pdfminer.six 依赖 | MIT；`charset_normalizer-3.5.2.dist-info/licenses/LICENSE` |
| cryptography | 50.0.2 | pdfminer.six 依赖 | Apache-2.0 OR BSD-3-Clause；`cryptography-50.0.2.dist-info/licenses/LICENSE`、`LICENSE.APACHE`、`LICENSE.BSD` |

其余依赖沿用基础 API 锁；例如 cryptography 的 cffi/pycparser 沿用基础锁中的版本。锁文件没有 PyMuPDF、fitz 或 OCRmyPDF；上述实际 Python requirement 链未声明 AGPL 包。不得把这一结论扩写成未审阅的全部原生代码都只有 MIT/BSD。

## 原生通知必须随制品保留

本节的具体文件清单来自 CPython 3.12、Windows x64 的已安装 wheels；Linux/其他架构须读取对应 wheel 的 `WHEEL`、`METADATA` 和许可证全文。相同 Python 版本号不保证原生二进制、ABI 或附带通知完全相同。

pypdfium2 的包自身文本位于 `pypdfium2-5.13.0.dist-info/licenses/LICENSES/`：`Apache-2.0.txt`、`BSD-3-Clause.txt`、`CC-BY-4.0.txt`。Windows 原生通知位于同一 `licenses/` 下的 `data/windows_x64/BUILD_LICENSES/`；该制品的 PDFium 版本为 `153.0.7999.0`。

| 实际原生通知文件 | 声明与保留要求 |
| --- | --- |
| `pdfium.txt`、`pdfium-binaries.txt` | PDFium 的 BSD 条款及附带 Apache 文本；二进制构建项目的 MIT 通知，保留各自全文 |
| `abseil.txt`、`llvm-libc.txt` | Apache-2.0；LLVM 文件含 LLVM Exceptions 及其他历史通知，不删除例外部分 |
| `agg23.txt`、`fast_float.txt`、`lcms.txt`、`simdutf.txt` | AGG 2.3 的原始宽松条款及其余组件的 MIT 类通知 |
| `freetype.txt` | FreeType Project License；保留二进制分发所需署名 |
| `icu.txt` | Unicode License V3 及附带构建宏等第三方通知，不能只留首段 |
| `libjpeg_turbo.ijg`、`libjpeg_turbo.md` | IJG、BSD 与相关 zlib 条款及使用说明，两份均保留 |
| `libopenjpeg.txt`、`libpng.txt`、`libtiff.txt`、`zlib.txt` | BSD-2-Clause、PNG Reference Library、TIFF 原始宽松条款和 zlib 通知 |

Pillow 的 `LICENSE` 另外包含这些实际组件章节：brotli 1.2.0、FreeType 2.14.3、HarfBuzz 14.2.1、lcms2 2.19.1、libavif 1.4.2、libjpeg-turbo 3.1.4.1、libpng 1.6.58、libwebp 1.6.0、OpenJPEG 2.5.4、TIFF 4.7.1、xz 5.8.3、zlib-ng 2.3.3。

其中 FreeType 明示 FTL/GPLv2 二选一，此处采用 FTL 路径并保留完整通知；xz 章节按库、工具及源码目录区分条款；ICU 通知含构建宏的 GPL 例外。出现 GNU/GPL 文字不能被删去，也不能仅凭关键词把整个 wheel 判为 AGPL。这些已读取的随附许可证未发现 Affero/AGPL 声明；这不替代未列出的编译依赖、外部系统库或未来制品的重新审阅。

用于复核本节 Windows 通知的文件摘要（针对原始文件字节）：

- Pillow `LICENSE`：`4f7866a74802c6326f81faff59a56546b6aec2b10b91973e0e9308de95e79857`。
- PDFium `pdfium.txt`：`961eacd9633fff6d051db7208b755e9210e30efac7adec3e6a6d52798f0ccf0e`。
- PDFium `freetype.txt`：`f4b133e25df1f86ad3ffea453aa0e613f0474f34778dbbb3e437e7b2724937d8`。

不要从 worker 镜像删除 `.dist-info/licenses/`，也不要将平台专属通知替换为上游仓库首页的单一许可证。升级、更换 wheel/原生构建或新增 OCR 引擎时，重新核对活跃 requirement 链、wheel 哈希、原生版本及完整通知；发现排除的许可证或版本无法取得时停止并报告。

## 独立验收入口

```sh
python -m pip install -r requirements-dev.lock
python -m pip install --require-hashes -r requirements-ocr.lock
python scripts/check_ocr_dependencies.py
python scripts/check.py ocr
```

`tests/ocr/test_pdf_dependencies.py` 使用强制 `ocr` 标记和函数内导入，真实验证固定版本、小型虚构 PDF 的公开创建/读取 API、pdfplumber 文本与 PDFium 有限尺寸渲染。借用的图像先复制，再关闭 bitmap/page/document，最后编码复制后的图像；pypdfium2 5.13 的 page/bitmap 使用公开 `close()`，不假定它们支持上下文管理器。

`scripts/check_ocr_dependencies.py` 在运行平台读取七个新增 distribution 的实际版本、许可声明与 wheel 标签，拒绝缺失许可证文件、版本漂移、已安装 PyMuPDF/fitz/OCRmyPDF，以及声明或通知中的 Affero/AGPL。输出仅含包内相对通知路径、SHA-256 和数量，不打印许可证全文或宿主路径；Linux OCR CI 的输出用于复核 Linux 制品，不借用本节的 Windows 文件摘要。

OCR CI 必须安装上述 hash 锁并实际运行此入口；不通过 `importorskip` 充当依赖验收。默认 API 检查与 OCR 检查分开执行，已有默认 API 的独立进程测试继续阻止 PDF/引擎库被 API 导入。Linux 子进程资源限制另由隔离测试实测，不能用 Windows 的依赖冒烟结果代替。

上游依据：[pypdf 6.19 公开配置 API](https://pypdf.readthedocs.io/en/6.19.0/modules/configuration.html)、[pdfplumber 0.11.10 依赖](https://github.com/jsvine/pdfplumber/blob/v0.11.10/requirements.txt)、[pypdfium2 5.13 原生许可证说明](https://github.com/pypdfium2-team/pypdfium2/blob/5.13.0/README.md#licensing)。制品实际内容仍优先于概括说明。
