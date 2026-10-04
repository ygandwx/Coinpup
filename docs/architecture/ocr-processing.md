# OCR 私有处理协议

本文件落实 [ADR 0019](decisions/0019-local-ocr-worker.md)，描述 worker 与受限子进程的内部协议；它不是业务 API。实际进度只见 [status.md](../engineering/status.md)，安装和运行边界见 [operations.md](../engineering/operations.md)。识别不产生分录，确认另按 T05-4 调用既有财务命令。

## 固定入口与原件

`run_isolated` 启动包内固定 bootstrap，先安装资源限制，再加载固定 `processor.process`。请求为 version=1、action=`inspect_pdf` 或 `prepare_pdf`、source 和可选 limits；只有 prepare_pdf 接受额外 prepare_limits。未知字段/动作/版本、布尔冒充整数或超硬上限均拒绝，不提供客户端可选择的命令、模块或解释器。inspect_pdf 的响应保持原样。

source 仅包含 `path`、`sha256`、`byte_size`：可信 worker 准备的随机私有 staging 绝对路径、小写 SHA-256 和严格正整数大小，至多 20 MiB。随机文件名为 32 位小写十六进制加 `.pdf`，不得用上传名称。Linux 检查私有父目录/文件、当前 UID、常规文件及 no-follow 边界；读到的实际长度和摘要必须匹配。Windows 可直接验证纯解析函数，不能由此声称具备 Linux 隔离或 ACL 保证。

领取事务必须先提交，随后才读原件和计算。后续 worker 从已有鉴权/descriptor 原件通道复制并验证 staging，禁止把数据库 URL、会话、租约 token 或原件全文传入处理环境。输入/输出 JSON 与管道总量有界，错误及 stderr 不显示原件、路径或 traceback。

## 文字层三态

响应始终含 `version`、`page_count`、`pages`、`reason_code`；每页含零基 `page_index`、`layer`、`route`、`reason_code`。整体不可读时 page_count=null、pages=[]，不从部分成功的页树推断其余页面无文字。

| layer | route | 含义 |
| --- | --- | --- |
| present | extract | 已观察到文字显示操作，后续仅提取 |
| absent | render | 完成受支持结构检查且确认没有文字显示操作，后续才允许渲染/OCR |
| unknown | manual | 无法证明文字层状态，转人工 |
| present | manual | 已发现文字，但另有解析疑点或不支持结构，保留事实并转人工 |

`Tj`、`TJ`、`'`、`"` 中任一文字显示操作即表示 present，空字串、乱码或不可见文字不例外；不以 `extract_text()` 返回空值判断 absent。只跟随实际 `Do` 调用的 Form，有限处理嵌套/循环；未调用的 Form 不构成页面文字。单纯 Image 不解码像素来判断文字层。

严格解析仍可能修复或跳过结构。pypdf 警告、异常、加密、预算超限，以及无法可靠处理的表单、注释、Pattern、SMask、未知操作等均不得进入 render；只保存稳定原因码，不保存原始诊断。INLINE IMAGE 的结束标记可被库无告警修复，Shading 也不在当前检查子集，两者先转人工；普通 Do Image 仍可证明无 PDF 文字操作。路由事实与最终字段质量分开：已有层提取失败、乱码或缺字段仍不得自动 OCR。

## 预算与后续处理

ProbeLimits 固定页数、页树深度/项数、流与文档解码字节、内容流数、操作数、operand 数/深度、Form 调用/深度、间接链、对象、资源项和过滤器数量上限；内部测试可下调，不能突破硬上限。worker 的冻结 processing 配置必须显式包含全部预算及处理版本，不依赖未来默认值；变更不覆盖已有任务配置。

Configuration 包住 Reader 全生命周期，限制已声明/解码流及页树，禁用外部 jbig2 命令。ContentStream.operations 仍会完整分配操作列表，pdfminer 的部分解压也没有可靠硬输出上限；应用预检不能替代子进程 AS/CPU/墙钟限制或后续容器内存边界。

prepare_pdf 仅将 present/extract 页交给 pdfplumber，保留原始文字和词框，不解析金额。空文字、明确解码缺损或超预算时整页转人工，不截断后冒充完整结果；unknown 页不调用提取或渲染库。字符、文字 UTF-8 字节、词数、词框 JSON 均有页/文档上限，完整响应默认至多 768 KiB；可下调至 128 字节，给固定失败 envelope 保留空间。

只有已证明 absent 的页才进一步检查几何和实际调用的 Image。初版支持有限 DeviceGray/RGB/CMYK、裸样本/Flate/JPEG 子集；复杂颜色、预测器和不支持过滤链转人工。JPEG 在像素解码前核对 SOF 尺寸和分量；裸样本/Flate 在既有流预算内核对解码样本长度。该检查仍不能替代 OS 对解压和原生库的限制。已有文字页不会因为附带图像不在这个子集而改走 OCR。

pypdf 先检查继承 MediaBox/CropBox、Rotate 和 UserUnit；非默认 UserUnit 暂转人工。PDFium 的实际有效尺寸必须一致，原生 bitmap maker 在分配前检查边长、页/文档像素和 RGBX 四字节预算。Rotate 只由页面自身应用一次；不额外旋转或再次裁剪。默认 200 DPI，允许 72–300；正式同机比较和 worker 配置须明确冻结 300 DPI，不从默认值推断实验配置。

prepare_pdf 每页额外含 text、words 和 raster，后者只含尺寸、stride、RGBX 和 DPI。位图在 rendered_page 上下文中供后续引擎消费，消费结束或异常均依次释放位图和页，文档由外层关闭；图像视图不得越过上下文生命周期。当前步骤不返回像素、base64、临时路径或识别字段，也不产生草稿或费用。

预算、失败路由和锁规则由虚构 PDF/图片、真实 Linux 子进程及后续恢复检查验证；依赖冒烟、探测结果和设计本身不能代替 OCR 准确率、部署或人工确认验收。
