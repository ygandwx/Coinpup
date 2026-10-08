# API 通用约定

接口路径、请求字段、响应字段及参数默认值以代码生成的 [OpenAPI](../../contracts/openapi.json) 为准。本文只记录跨接口规则；设计理由由 [ADR 索引](decisions/README.md) 路由，不用历史回执或历史 ADR 的交付边界判断当前任务状态。

## 鉴权与归属

- 浏览器通过同源 `/api/v1/` 请求使用服务器会话。会话 Cookie 为 HttpOnly、SameSite=Strict，生产启用 Secure；登录验证允许的精确 Origin，已认证写操作同时需要有效会话、允许的 Origin 和 `X-CSRF-Token`。SameSite 不能替代来源或 token 检查。来源：[ADR 0002](decisions/0002-administrator-session.md)、[ADR 0004](decisions/0004-owned-ledger-structure.md)。
- CSRF token 由会话接口取得，只保存在客户端内存；401 隐藏私人工作区并返回登录，同一用户重新登录后由用户手动恢复待确认操作。主动退出或更换用户清理原意图并忽略迟到响应。来源：[ADR 0002](decisions/0002-administrator-session.md)、[ADR 0010](decisions/0010-financial-web-and-retry.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。
- 服务从已验证会话确定访问所有者，客户端不能指定所有者。账户、分类、业务、上传和文件关联必须属于相应账本；跨账本引用被拒绝，不能凭名称或相同文件摘要合并主体数据。来源：[ADR 0004](decisions/0004-owned-ledger-structure.md)、[ADR 0012](decisions/0012-private-files-and-consistent-bundles.md)。
- 管理员初始化和密码重置通过服务器 CLI；没有公开注册或 HTTP 初始化入口。浏览器会话合同不能直接作为完整移动端身份协议。来源：[ADR 0002](decisions/0002-administrator-session.md)。

## 金额与财务事实

- 金额输入和输出使用十进制字符串；拒绝 JSON 数值、指数、千分位、空白及超资产精度输入，即使多余小数位为零也拒绝。输出按资产精度固定小数位，负零统一为零。服务用整数最小单位处理数量，网页用 BigInt，金额不能经过 float、`Number` 或 `parseFloat`。来源：[ADR 0003](decisions/0003-exact-asset-amounts.md)、[ADR 0010](decisions/0010-financial-web-and-retry.md)。
- 资产以完整身份和精度确定金额单位，不同网络的同名代币不能相加。代码、网络、代币标识和精度创建后不可原地修改；稳定币需要显式配置，新资产须关联到付款账户。来源：[ADR 0003](decisions/0003-exact-asset-amounts.md)、[ADR 0004](decisions/0004-owned-ledger-structure.md)、[ADR 0010](decisions/0010-financial-web-and-retry.md)。
- 期初数量非零，可以为负；每账户/资产只建立一次，不计收入，取消后仍保留占用标记。收支的本金和分类拆分为正，拆分分类不重复，合计等于本金，单分类也使用拆分列表。交易日与业务归属日是日历日期字符串。来源：[ADR 0005](decisions/0005-atomic-posting-and-receipts.md)、[ADR 0008](decisions/0008-operation-revisions-and-cancellation.md)、[ADR 0010](decisions/0010-financial-web-and-retry.md)。
- 同资产转账要求同账本两个不同账户；信用卡消费属于支出，还款属于转账，本金不重复计费。换汇保存不同资产各自的实际数量，可以使用同一多资产账户；本金逐资产配平，不作为收入或费用，不由行情改写。来源：[ADR 0006](decisions/0006-same-asset-transfers.md)、[ADR 0007](decisions/0007-exchanges-and-explicit-fees.md)。
- 收入、支出、转账和换汇可以附加独立手续费，最多 20 项；每项明确扣费账户、资产、正数数量和费用分类，允许同账本第三账户或第三资产，期初不带费用。本金与全部费用在同一事务提交，任一步失败整体回滚。来源：[ADR 0007](decisions/0007-exchanges-and-explicit-fees.md)。
- 原币余额包含已归档账户及停用资产或关联，并保留状态标志。账户余额由全部账户分录计算，不用最近一张凭证替代；不同资产不直接求和。来源：[ADR 0005](decisions/0005-atomic-posting-and-receipts.md)、[ADR 0008](decisions/0008-operation-revisions-and-cancellation.md)、[ADR 0009](decisions/0009-business-web-workspace.md)。

## 修改、归档与版本

资金入口只包含 money 账户；往来余额使用独立只读入口，按控制身份、原资产及全部维度分组，保持有符号数量，不跨方向、对手方或单据抵销。内部命令按固定身份自动管理控制账户/资产关联，禁止通用表单创建、改类或直接调余额；归档/停用只读保留，不自动恢复。来源：[ADR 0016](decisions/0016-account-classes-and-dimensions.md)、[ADR 0022](decisions/0022-control-account-implementation.md)。

- 版本更新必须携带冻结的 `expected_version`；成功后版本递增，过期版本返回 409 `version_conflict`。客户端保留草稿和原版本，只有明确重新载入才替换；语言切换不改变业务值或草稿。来源：[ADR 0004](decisions/0004-owned-ledger-structure.md)、[ADR 0009](decisions/0009-business-web-workspace.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。
- 归档保留记录、原币数量和历史引用；主体归档后允许读取，暂停新的账本写入。已经成功的财务命令和已完成上传可以重放，不能因后续归档或停用丢失回执。来源：[ADR 0004](decisions/0004-owned-ledger-structure.md)、[ADR 0005](decisions/0005-atomic-posting-and-receipts.md)、[ADR 0012](decisions/0012-private-files-and-consistent-bundles.md)。
- 主体/账本、账户和分类创建支持稳定客户端 UUID，重复 ID 是冲突，不覆盖已有数据，也不等同财务命令重放；未知创建结果先读取核对。分类模板复制为各主体独立分类，界面语言不自动翻译已保存名称，公司资料与账户资料按各自归属保存。来源：[ADR 0004](decisions/0004-owned-ledger-structure.md)、[ADR 0009](decisions/0009-business-web-workspace.md)、[ADR 0010](decisions/0010-financial-web-and-retry.md)。
- 更正保持业务 ID 和种类，完整冲销旧本金与全部费用再写入替代分录；取消只冲销，终止后不能重新激活。命令需要当前版本、非空原因和完整替代内容，替代内容不带业务 ID；期初账户/资产固定。来源：[ADR 0008](decisions/0008-operation-revisions-and-cancellation.md)、[ADR 0011](decisions/0011-financial-revision-web.md)。
- 原始分录、封存行和回执不可修改或删除；更正/取消保持连续版本链。旧账户、分类或资产失效后仍可精确冲销，替代内容的新引用须有效；历史按版本保留原因、执行人及精确有符号分录。来源：[ADR 0005](decisions/0005-atomic-posting-and-receipts.md)、[ADR 0008](decisions/0008-operation-revisions-and-cancellation.md)。

## 幂等与回执

- 财务写命令必须带 `Idempotency-Key`：1–128 个不含空格的可见 ASCII 字符，作用域为账本。同键同请求返回永久保存的原回执；同键不同请求返回 409，失败事务不保存成功回执。来源：[ADR 0005](decisions/0005-atomic-posting-and-receipts.md)、[ADR 0008](decisions/0008-operation-revisions-and-cancellation.md)。
- 旧回执按其 v1 版本使用冻结的验证后字段和默认值；新回执一律使用 v2，摘要包含收到的原始 JSON、动作、固定路由模板及账本 ID，更正/取消还包含路径中的业务 ID。JSON 键顺序和空白不影响摘要；金额字符串拼写与数组顺序保留，`"1.0"` 与 `"1.00"` 不同。来源：[ADR 0014](decisions/0014-versioned-command-hashes.md)。
- 重试使用原完整正文与原键，不从可变表单重建，也不把服务端生成的 ID 补进原先省略 ID 的请求。v2 中省略字段、显式 null 或空数组属于不同正文；v1 继续保留省略可选 ID 与 null 等价、原收支/转账省略费用与 `fees: []` 等价的历史规则。无费用回执不补写 `fees`。来源：[ADR 0014](decisions/0014-versioned-command-hashes.md)、[ADR 0010](decisions/0010-financial-web-and-retry.md)。
- 新增及其重放返回原 201 回执，更正/取消及其重放返回原 200 状态回执。读取接口返回当前状态，可能已比回执更新；收到回执后另读当前状态，不用旧回执覆盖新版本。来源：[ADR 0005](decisions/0005-atomic-posting-and-receipts.md)、[ADR 0008](decisions/0008-operation-revisions-and-cancellation.md)。

## 读取与分页

- 有界列表返回数组，`limit` 为 1–200、默认 100，`offset` 为 0–100000、默认 0。采用各接口规定的稳定排序，历史按版本升序；一页未见不代表记录不存在，这些参数不是增量同步游标。来源：[ADR 0004](decisions/0004-owned-ledger-structure.md)、[ADR 0005](decisions/0005-atomic-posting-and-receipts.md)、[ADR 0011](decisions/0011-financial-revision-web.md)。
- 是否包含归档/停用项由接口参数控制，默认值以 OpenAPI 为准；财务列表默认保留全部状态，可显式筛选 active、cancelled 或 all。文件目录和流水证据列表分别分页，不能用一页目录判定关联原件不存在。来源：[ADR 0008](decisions/0008-operation-revisions-and-cancellation.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。
- 流水列表 `order` 默认 `created_at`，按创建时间、业务 ID 倒序；可选 `transaction_date` 按当前入账凭证的交易日、创建时间、业务 ID 倒序。`from_date` 和 `to_date` 使用严格 `YYYY-MM-DD`，按同一当前凭证的交易日作闭区间筛选；反向区间返回 422。已取消业务仍使用最后一张入账凭证的交易日。网页显式选择交易日排序，不改变 API 的默认响应。
- 变更通知通过 `GET /api/v1/changes` 按所有者过滤，`after` 默认 `"0"`、`limit` 默认 100（1–200），返回 `changes` 和 `next_cursor`。`seq`/游标是精确十进制字符串；非空页只推进至最后交付的序号，空页保留原游标，序号允许回滚空洞。归档/停用、恢复及取消分别通知为 archive、restore、cancel，记录本身保留。来源：[ADR 0015](decisions/0015-ordered-change-log.md)。
- 首次同步先取得起始游标，再分页读取全量对象，最后从该游标读取后续通知；允许重复获取同一对象。资产使用完整文本 ID，account_assets 使用 `account_uuid:asset_id` 并要求重读父账户，不能因父版本相同而忽略关联变更。通知用于重读当前记录；设备、离线写入和冲突处理由后续完整同步协议定义。来源：[ADR 0015](decisions/0015-ordered-change-log.md)。

## 错误与客户端处理

- 领域错误使用 `{"detail":{"code":"…","message":"…"}}`，客户端按稳定 `code` 和 HTTP 状态判断；消息不回显私人输入、SQL、连接信息或文件路径。未知 HTTP 错误不能凭文本推断已经提交或取消。来源：[ADR 0004](decisions/0004-owned-ledger-structure.md)、[ADR 0009](decisions/0009-business-web-workspace.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。
- 请求校验 422 的 `detail` 是仅含 `loc`、`type`、通用 `msg` 的错误列表；会话错误及通用异常的 `detail` 也可能是字符串，不假定所有错误都有业务 `code`。客户端只保留字段位置、类型及通用消息，不复制原始输入或校验上下文。来源：[ADR 0002](decisions/0002-administrator-session.md)、[ADR 0004](decisions/0004-owned-ledger-structure.md)、[实际错误边界](../../services/api/src/coinpup_api/main.py)。
- 401 要求重新登录，403 表示来源/CSRF 或访问保护失败，409 表示版本或内容冲突；网络中断、超时、服务器错误及无法验证的成功响应均可能留下未知写结果，不能自动换键或换上传 ID。来源：[ADR 0002](decisions/0002-administrator-session.md)、[ADR 0010](decisions/0010-financial-web-and-retry.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。

## 私有原件协议

- 两步上传：先在账本内预留稳定上传 UUID、原文件名、长度和可选流水引用，再以 `application/octet-stream` PUT 完整原件；写鉴权先于读流。同预留 ID/元数据返回原预留，改变请求冲突；拒绝压缩传输，实际字节长度也要符合预留与限额。来源：[ADR 0012](decisions/0012-private-files-and-consistent-bundles.md)。
- 限额和支持格式从配置接口读取；PDF/JPEG/PNG/WebP 的格式边界检查不代表完整解析或恶意内容扫描。没有分块续传，重试须重传相同完整原件；已完成上传仍验证字节大小与摘要。来源：[ADR 0012](decisions/0012-private-files-and-consistent-bundles.md)。
- SHA-256 与大小只在同账本内查重；重复提示不是业务重复判断，不返回其他账本身份。上传和证据关联不创建费用，原件与关联可以分别归档，流水取消或更正仍保留证据。来源：[ADR 0012](decisions/0012-private-files-and-consistent-bundles.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。
- 下载只能走鉴权接口，不把私有目录映射为静态网址。服务器以已验证的打开句柄输出 attachment，附 no-store、nosniff 与 sandbox CSP；浏览器核对大小、媒体类型及 SHA-256，再创建并及时释放 ObjectURL。来源：[ADR 0012](decisions/0012-private-files-and-consistent-bundles.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。
- 文件存储位置、上传超时配置及数据库/原件一致 bundle 的操作命令见 [运维说明](../engineering/operations.md)；数据库单独备份不包含原件。上传、识别与财务确认具有分别的生命周期。来源：[ADR 0012](decisions/0012-private-files-and-consistent-bundles.md)。

## 网页未知结果

- OCR 草稿列表仅返回分页元数据，支持任务/状态筛选；详情经会话及账本归属校验后才返回原始识别证据、当前复核字段和冻结策略。读取不写状态、不自动确认或入账；归档后历史仍可读。任务原文与草稿属于私有数据，响应 no-store，不含租约、存储路径或数据库配置。缺少新版原文/策略的历史候选明确返回 null，不伪造证据。接口字段以 OpenAPI 为准。

- OCR 创建冻结所有者、账本、原件和稳定 `intent_id`，同意图返回任务当前视图，改变原件冲突；首次配置永久保留，不能随服务器默认值升级。未配置处理器时仍先核归属及旧意图，新建才返回 `ocr_unavailable`。创建响应丢失可按原意图查询并重发；404 不能证明未创建，不静默换意图。任务 pending 只表示排队，上传或排队不产生费用。来源：[ADR 0019](decisions/0019-local-ocr-worker.md)。
- OCR 显式重试沿用原任务和配置，并要求当前版本；尝试次数不清零。重试响应未知时先查询，不能自动用新版本再次重排。状态查询不暴露租约 token、私有路径或原始全文，草稿与人工确认使用独立协议。来源：[ADR 0019](decisions/0019-local-ocr-worker.md)。

- 财务命令和原件上传分别由应用内存控制器保留一个未解决意图；冻结用户、账本、UUID、键/预留元数据和原正文或 File，不写入浏览器持久存储。重复点击共享请求，待确认时限制新命令和导航；刷新/关闭后先核对服务端列表。来源：[ADR 0010](decisions/0010-financial-web-and-retry.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。
- 新增命令可按客户端 UUID 核对当前业务；404 不能证明原提交失败。修订针对既有 UUID，查询任何版本都不能单独确认本次修订，必须以原 POST/原键取得回执；只有明确 version_conflict 加随后更高当前版本的组合证据才可解除该旧修订意图。来源：[ADR 0010](decisions/0010-financial-web-and-retry.md)、[ADR 0011](decisions/0011-financial-revision-web.md)。
- 上传只有相同冻结预留、ready 状态和匹配回执能经查询确认；pending、404、身份不符或无效响应保留待确认。已知字节冲突跨重新登录保留，不能以同文件名/大小误认成功；停止只中断浏览器等待，不能表示服务端取消。来源：[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。
- 未知财务结果保留原命令重试，未知上传结果保留原上传 ID 和完整 File 重试；401 仅隐藏内容，同用户登录后手动继续，异用户/明确退出释放。刷新或关闭丢失内存意图，不具有持久离线队列语义。来源：[ADR 0010](decisions/0010-financial-web-and-retry.md)、[ADR 0011](decisions/0011-financial-revision-web.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。


## 可选期间关闭

`GET /ledgers/{ledger_id}/period` 默认开放、版本1；只有显式关闭才产生状态。可同时提供 `from_date`/`to_date` 与 `date_basis`（transaction/recognition），返回所选区间 open/closed/partial、生成时间及与区间相交的最近重开审计；未选区间时 `range_status` 为空。此为T08的状态基础，不生成或保存报表快照。

`POST /ledgers/{ledger_id}/period-changes` 使用独立账本作用域 Idempotency-Key 和原JSON v2摘要，提交 action（close/reopen）、必填可空 closed_through、expected_version、非空 reason。重开必须显式缩小截止日或解除关闭；原键优先重放，版本过期为409 version_conflict。未知提交保留原键与原JSON，不能刷新版本后悄悄重试。`GET` 同路径按版本倒序分页读取不可变审计（limit 1–200，offset 0–100000）。详见ADR0017/0023。

新财务命令任一日期在已关闭范围返回409 period_closed；更正检查原凭证与替代凭证，取消检查原凭证。旧成功回执仍永久重放。未启用结账的账本保持原行为。
