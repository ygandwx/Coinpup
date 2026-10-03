# ADR 0012：私有文件、可重试上传与一致恢复

日期：2026-10-04。任务：T05-1。状态：实现中，实际验证见[当前状态](../../engineering/status.md)。

## 背景与决定

票据原件、公司证件与账务记录需要分别保存生命周期。上传文件不代表发生费用，识别文字也不能直接入账。首先建立本地私有存储、账本归属、原件关联和数据库与文件共同恢复，再实现网页与本地 OCR。

使用 PostgreSQL 元数据与服务器私有文件目录，不引入外部对象存储或云 OCR。文件按账本隔离；内容相同只在同一账本复用文件，不能返回其他账本的文件 ID 或关联。账本内 PDF/JPEG/PNG/WebP 以 SHA-256 与大小查重，重复提示不等于判断业务重复。

## 上传协议

登录后 `GET /api/v1/files/configuration` 返回实际限额与支持格式。在 `/api/v1/ledgers/{ledger_id}` 下：

1. `POST /uploads` 保存客户端稳定 UUID、`original_filename`、`declared_size` 与可选 `operation_id`。同 ID、同内容返回原 reservation；更改请求返回冲突。
2. `PUT /uploads/{id}/content` 以 `application/octet-stream` 传输完整字节。Cookie、Origin、CSRF 与账本归属检查先于读流；拒绝压缩传输，实际累计字节也必须符合预留长度和限额。
3. `GET /uploads/{id}` 返回 `pending` 或 `ready`，完成后附不可变回执。丢失响应可查询，再以相同预留与原件重试；不得重新生成 ID 掩盖未知结果。

默认 50 MiB、120 秒，通过 `COINPUP_MAX_UPLOAD_BYTES` 与 `COINPUP_UPLOAD_TIMEOUT_SECONDS` 配置。没有分块续传；中断后重传完整原件。已完成上传的重试也必须验证实际字节摘要与长度，不能只根据请求头或大小返回成功。已完成回执可在主体归档后重放；新的写入要求主体有效。

前缀与尾部格式检查只是格式边界，不能证明 PDF 或图片可以完整解析，也不能声称已经扫描恶意内容。后续 worker 需单独限制页数、像素、时间与内存，并把解析失败反馈为待处理状态。

## 持久化与并发

文件名仅用于显示和下载名，不参与文件路径。`COINPUP_FILES_DIRECTORY` 下只有随机 32 位小写十六进制的 `blobs/<key>` 和临时 `staging/<token>`。不挂载为静态资源。Linux 使用私有权限、目录描述符、no-follow、常规文件检查、文件及目录 fsync。下载先鉴权，再核对实际文件大小和 SHA-256，使用已打开的句柄输出，附 attachment、no-store、nosniff 与 sandbox CSP。

长时间收流、摘要、文件写入和 fsync 均在数据库事务外。完整文件先持久发布，再用短事务依次锁账本与主体、重新核对归属/归档/查重状态，原子保存文件、关联与上传回执。并发重复只产生一个同账本规范文件。并发竞争或数据库结果未知可留下不可访问的孤立 blob；本增量不做垃圾回收，不能为清理空间删除可能已提交的原件。

三张表：`file_uploads` 保存冻结的上传请求和完成回执；`stored_files` 保存不可变字节身份与可版本更新的标题/归档状态；`operation_file_links` 保存同账本原件与财务操作的关联及归档版本。复合外键约束账本/所有者与关联归属。禁止硬删与修改已完成上传，标题/归档修改要求版本严格递增。存在历史时拒绝迁移降级，避免借降级丢失原件关联。

列表、读取、标题/归档 PATCH 与流水关联 API 在相同账本前缀下。PATCH 带 `expected_version`，冲突不能覆盖新版本。关联只附加证据，不创建、修改或冲销财务金额；取消的流水仍可以保存证据。

## 备份与恢复

保留原 `database-only` 工具的明确边界，另建 `database-and-files` v2 bundle。PostgreSQL 17 的只读 REPEATABLE READ 事务导出 snapshot，同一 snapshot 读取全部 `stored_files` 行并由 `pg_dump --snapshot` 备份数据库。导出事务维持至 dump 完成。原件不可变且暂不垃圾回收，因此随后复制该快照引用的全部原件（含归档），逐个核对摘要和大小；不扫描临时文件或孤立 blob。

manifest 最后持久发布，缺少它不算备份成功。恢复只允许明确确认的新空隔离数据库及不存在的新文件目录，先校验完整 bundle，再复制原件、执行单事务 `pg_restore`，最后逐行核对恢复的文件元数据。失败不自动清理目标或切换应用；生产切换仍需独立验收。具体命令与限制见[运维说明](../../engineering/operations.md)。

## 备选方案与影响

直接 multipart 上传会增加额外解析依赖，并更难把读取请求体之前的鉴权边界表示清楚；使用两步原始流。仅数据库备份无法恢复原件；先后独立备份又会遗漏并发提交，所以使用明确共享快照。文件系统与 PostgreSQL 没有共享事务，选择先持久文件后提交元数据，并保留无法确认是否被引用的文件。

本增量不提供上传页面、OCR、自动入账、公司证件到期提醒或 App 离线文件缓存。T05-2 接网页，后续 worker 处理识别和人工确认；T10 完善保留策略、异地副本与生产恢复演练。

技术依据：[PostgreSQL 17 pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html)、[快照同步函数](https://www.postgresql.org/docs/17/functions-admin.html#FUNCTIONS-SNAPSHOT-SYNCHRONIZATION)。
