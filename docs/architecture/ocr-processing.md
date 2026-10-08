# OCR 私有处理协议

本文件落实 [ADR 0019](decisions/0019-local-ocr-worker.md)及其建议值策略后继 [ADR 0020](decisions/0020-ocr-prefill-and-review.md)，描述 worker 与受限子进程的内部协议；它不是业务 API。实际进度只见 [status.md](../engineering/status.md)，安装和运行边界见 [operations.md](../engineering/operations.md)。识别不产生分录，确认另按 T05-4 调用既有财务命令。

## 固定入口与原件

`run_isolated` 启动包内固定 bootstrap，先安装资源限制，再加载固定 `processor.process`。version=1；inspect_pdf 接受 source/limits，prepare_pdf 另接受 prepare_limits；prepare_image 接受 source/media_type/prepare_limits/image_limits，不接受 PDF 的 limits。未知字段/动作/版本、布尔冒充整数或超硬上限均拒绝，不提供客户端可选择的命令、模块或解释器。inspect_pdf 的响应保持原样。

source 仅包含 `path`、`sha256`、`byte_size`：可信 worker 准备的随机私有 staging 绝对路径、小写 SHA-256 和严格正整数大小，至多 20 MiB。随机文件名为 32 位小写十六进制；PDF 后缀 .pdf，图片按固定 media_type 使用 .jpg/.png/.webp，不得用上传名称。Linux 检查私有父目录/文件、当前 UID、常规文件及 no-follow 边界；读到的实际长度和摘要必须匹配。Windows 可直接验证纯解析函数，不能由此声称具备 Linux 隔离或 ACL 保证。

领取事务必须先提交，随后才读原件和计算。后续 worker 从已有鉴权/descriptor 原件通道复制并验证 staging，禁止把数据库 URL、会话、租约 token 或原件全文传入处理环境。输入/输出 JSON 与管道总量有界，错误及 stderr 不显示原件、路径或 traceback。

`run_isolated` 的可选 heartbeat 仅在父进程执行：启动前、管道等待循环及收集输出后检查，必须明确返回 true。worker 在回调内自行按租约周期节流，以有超时的短事务续租，不持事务等待识别；回调异常、失租或归档均取消计算。取消沿用完整进程组终止、等待与临时文件清理，返回稳定 processing_cancelled，不接受已收集的晚到结果；清理失败仍优先返回 processing_cleanup_failed。回调、租约凭据和数据库连接不传子进程，最终数据库写回仍必须校验 token/generation 和到期时间。

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

## 图片准备

prepare_image 只接受现有 JPEG/PNG/WebP 三种 media_type，签名、固有尺寸和实际 Pillow format 必须一致。有限 JPEG marker、PNG chunk/CRC、WebP RIFF 检查先于 Image.open；不扩展原件接口，不把上传签名检查当作解码保证。动画、多帧、ICC/XMP、复杂压缩文字 metadata 及当前子集外的结构转人工；普通灰度、RGB/CMYK、PNG palette 和 alpha 在实际 codec 中验证。

WebP 的 Pillow open 会创建原生动画解码器及两块画布，不能称为只读头。先核验完整 RIFF 长度、有限 chunk/padding、唯一 primary，以及 VP8X canvas 与 VP8/VP8L 固有尺寸；在原生构造前检查 8×像素预算。图像共用像素/边长/最终 RGBX 字节限额；另以 ImageLimits 限工作缓冲 256 MiB、metadata 64 KiB、chunk 128，仅可下调。普通/WebP 分别预收 16/24×像素的保守工作估算，不能将此估算称为实测 RSS 或 OS 硬上限。A4 300 DPI 在默认限额内，正式配置仍须显式冻结。

EXIF 方向仅应用一次，透明像素按白底合成；不缩放、不猜原图 DPI，raster.dpi=null，坐标使用方向纠正后的像素。prepared_image 上下文内供后续引擎消费 live PIL RGBX，退出或消费者异常时关闭全部图像及输入流，丢弃 metadata；内部解码 warning 转人工，消费者不继承该 warning 策略。响应只有单页路由和 raster 元数据，不返回像素、私有路径或候选字段。

## 共享字段解析

field_parser 的 parse_document 只接受 TextPage/Word 中的实际文字、页面尺寸和词框；不接收文件名、模板、真值、预期行数、账户配置或财务命令。它与引擎适配分离，纯标准库，不访问数据库、不产生分录。输入先检查类型、有限坐标、字符下界再编码，并累计页/词/文字/行/表/字段/证据/输出预算；超限整份转人工，不截断结果冒充完整。输出失败 envelope 也受最小 128 字节预算约束。

读取顺序只由物理坐标决定；重叠或跨列不确定时转人工并保留流水行序号。公开中英标签整体匹配，Subtotal 不当作 Total；坏行保留三字段槽，相同金额交易不去重。只有可见连续打印页码、同列和重复 header 无冲突才能在同一物理图片里接续表，物理 PDF 页重新编号；缺少再次打印的字段沿用此前可见 scope。

金额使用有界 ASCII 十进制字符串和整数运算，日期只接受有效 ISO，初版确定币种为显式 CNY/USD/GBP/EUR/HKD；其它币种和符号保留原文转人工。规范金额、字段身份、原始证据和评分规则见[同机协议](../engineering/ocr-benchmark.md)。字段 certain 只表示解析明确，仍须用户确认；非零额外精度、冲突/缺失字段为 review。小计加税及逐币种余额恒等式只诊断，不补值、不修金额、不借邻行填缺失字段。

预算、失败路由和锁规则由虚构 PDF/图片、真实 Linux 子进程及后续恢复检查验证；依赖冒烟、探测结果和设计本身不能代替 OCR 准确率、部署或人工确认验收。
