# API 通用约定

已确认经营单据的 `/history` 只分页返回按单据版本递增的确认/更正/取消元数据，并为修订提供独立的 `previous_line_count` / `replacement_line_count`（包含零金额行，取消替代数为0，原确认两者为空，其行数在原单据中）。详情显示须逐一核对这两个计数；首版使用真实确认版本，不固定为1。`/revisions/{revision_id}` 返回该版不可变200，核对其原请求回执、完整前后计价事实与冲销/应计映射；跨单据或账本先拒绝。目录、当前快照及固定历史详情都不是未知命令的成功证明；原意图仍按原键和JSON重试。

已确认经营单据的 `GET business-documents/{document_id}/current` 在同一只读快照返回当前完整计价行与正金额行的应计操作版本。取消后保留单据/确认身份和终态版本，当前单据与应计映射为空；原 `/confirmation` 始终保留原201事实。当前快照用于审阅新意图，不能证明未知写入成功；已归档、资产停用及期间关闭不屏蔽历史读取。依据[ADR0050](decisions/0050-confirmed-document-revisions.md)。

接口路径、请求字段、响应字段及参数默认值以代码生成的 [OpenAPI](../../contracts/openapi.json) 为准。本文只记录跨接口规则；设计理由由 [ADR 索引](decisions/README.md) 路由，不用历史回执或历史 ADR 的交付边界判断当前任务状态。

## 鉴权与归属

- 浏览器通过同源 `/api/v1/` 请求使用服务器会话。会话 Cookie 为 HttpOnly、SameSite=Strict，生产启用 Secure；登录验证允许的精确 Origin，已认证写操作同时需要有效会话、允许的 Origin 和 `X-CSRF-Token`。SameSite 不能替代来源或 token 检查。来源：[ADR 0002](decisions/0002-administrator-session.md)、[ADR 0004](decisions/0004-owned-ledger-structure.md)。
- CSRF token 由会话接口取得，只保存在客户端内存；401 隐藏私人工作区并返回登录，同一用户重新登录后由用户手动恢复待确认操作。主动退出或更换用户清理原意图并忽略迟到响应。来源：[ADR 0002](decisions/0002-administrator-session.md)、[ADR 0010](decisions/0010-financial-web-and-retry.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。
- 服务从已验证会话确定访问所有者，客户端不能指定所有者。账户、分类、业务、上传和文件关联必须属于相应账本；跨账本引用被拒绝，不能凭名称或相同文件摘要合并主体数据。来源：[ADR 0004](decisions/0004-owned-ledger-structure.md)、[ADR 0012](decisions/0012-private-files-and-consistent-bundles.md)。
- 管理员初始化和密码重置通过服务器 CLI；没有公开注册或 HTTP 初始化入口。浏览器会话合同不能直接作为完整移动端身份协议。来源：[ADR 0002](decisions/0002-administrator-session.md)。

## 金额与财务事实

- 金额输入和输出使用十进制字符串；拒绝 JSON 数值、指数、千分位、空白及超资产精度输入，即使多余小数位为零也拒绝。输出按资产精度固定小数位，负零统一为零。服务用整数最小单位处理数量，网页用 BigInt，金额不能经过 float、`Number` 或 `parseFloat`。来源：[ADR 0003](decisions/0003-exact-asset-amounts.md)、[ADR 0010](decisions/0010-financial-web-and-retry.md)。
- 资产以完整身份和精度确定金额单位，不同网络的同名代币不能相加。代码、网络、代币标识和精度创建后不可原地修改。`GET /api/v1/assets/presets` 返回原生资产和经核对的主网代币预设，带来源与核对日期。新增资产仍须显式保存并关联到付款账户。调用方使用返回的 `asset_id`，保留旧资产原 ID，新原生资产使用 `native:<network>:<code>`。相关规则见 [ADR 0003](decisions/0003-exact-asset-amounts.md)、[ADR 0004](decisions/0004-owned-ledger-structure.md) 和 [ADR 0010](decisions/0010-financial-web-and-retry.md)。
- 期初数量非零，可以为负；每账户/资产只建立一次，不计收入，取消后仍保留占用标记。收支的本金和分类拆分为正，拆分分类不重复，合计等于本金，单分类也使用拆分列表。交易日与业务归属日是日历日期字符串。来源：[ADR 0005](decisions/0005-atomic-posting-and-receipts.md)、[ADR 0008](decisions/0008-operation-revisions-and-cancellation.md)、[ADR 0010](decisions/0010-financial-web-and-retry.md)。
- 同资产转账要求同账本两个不同账户；信用卡消费属于支出，还款属于转账，本金不重复计费。换汇保存不同资产各自的实际数量，可以使用同一多资产账户；本金逐资产配平，不作为收入或费用，不由行情改写。来源：[ADR 0006](decisions/0006-same-asset-transfers.md)、[ADR 0007](decisions/0007-exchanges-and-explicit-fees.md)。
- 收入、支出、转账和换汇可以附加独立手续费，最多 20 项；每项明确扣费账户、资产、正数数量和费用分类，允许同账本第三账户或第三资产，期初不带费用。本金与全部费用在同一事务提交，任一步失败整体回滚。来源：[ADR 0007](decisions/0007-exchanges-and-explicit-fees.md)。
- 原币余额包含已归档账户及停用资产或关联，并保留状态标志。账户余额由全部账户分录计算，不用最近一张凭证替代；不同资产不直接求和。来源：[ADR 0005](decisions/0005-atomic-posting-and-receipts.md)、[ADR 0008](decisions/0008-operation-revisions-and-cancellation.md)、[ADR 0009](decisions/0009-business-web-workspace.md)。

## 修改、归档与版本

资金入口只包含 money 账户；往来余额使用独立只读入口，按控制身份、原资产及全部维度分组，保持有符号数量，不跨方向、对手方或单据抵销。内部命令按固定身份自动管理控制账户/资产关联，禁止通用表单创建、改类或直接调余额；归档/停用只读保留，不自动恢复。来源：[ADR 0016](decisions/0016-account-classes-and-dimensions.md)、[ADR 0022](decisions/0022-control-account-implementation.md)。

- 版本更新必须携带冻结的 `expected_version`；成功后版本递增，过期版本返回 409 `version_conflict`。客户端保留草稿和原版本，只有明确重新载入才替换；语言切换不改变业务值或草稿。来源：[ADR 0004](decisions/0004-owned-ledger-structure.md)、[ADR 0009](decisions/0009-business-web-workspace.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。
- 归档保留记录、原币数量和历史引用；主体归档后允许读取，暂停新的账本写入。已经成功的财务命令和已完成上传可以重放，不能因后续归档或停用丢失回执。来源：[ADR 0004](decisions/0004-owned-ledger-structure.md)、[ADR 0005](decisions/0005-atomic-posting-and-receipts.md)、[ADR 0012](decisions/0012-private-files-and-consistent-bundles.md)。
- 主体/账本、账户和分类创建支持稳定客户端 UUID，重复 ID 是冲突，不覆盖已有数据，也不等同财务命令重放；未知创建结果先读取核对。分类模板复制为各主体独立分类，界面语言不自动翻译已保存名称，公司资料与账户资料按各自归属保存。来源：[ADR 0004](decisions/0004-owned-ledger-structure.md)、[ADR 0009](decisions/0009-business-web-workspace.md)、[ADR 0010](decisions/0010-financial-web-and-retry.md)。
- 往来单位与项目创建必须提供稳定客户端UUID；重复ID返回409 `duplicate_record`，未知创建结果读取同账本同ID核对，不覆盖已有资料，也不产生财务回执。更新保留原`expected_version`，旧匿名往来须同时补全名称与角色；资料变更不改历史分录。同名项目不自动合并。来源：[ADR 0024](decisions/0024-business-master-data.md)。
- 更正保持业务 ID 和种类，完整冲销旧本金与全部费用再写入替代分录；取消只冲销，终止后不能重新激活。命令需要当前版本、非空原因和完整替代内容，替代内容不带业务 ID；期初账户/资产固定。来源：[ADR 0008](decisions/0008-operation-revisions-and-cancellation.md)、[ADR 0011](decisions/0011-financial-revision-web.md)。
- 原始分录、封存行和回执不可修改或删除；更正/取消保持连续版本链。旧账户、分类或资产失效后仍可精确冲销，替代内容的新引用须有效；历史按版本保留原因、执行人及精确有符号分录。来源：[ADR 0005](decisions/0005-atomic-posting-and-receipts.md)、[ADR 0008](decisions/0008-operation-revisions-and-cancellation.md)。

## 真实退款

退款创建和更正按请求中的顺序关联退回。服务持写锁核对每个退回的当前版本及日期，并永久保存所引用的版本。已取消的退回返回 `409 operation_cancelled`，缺失及跨账本、跨付款的引用返回 `404`。已有回执按原幂等键重放。

退款独立记录实际资金，不自动冲减经营收入/费用；贷项独立处理。新意图明确原付款版本、实际账户/资产数量、原资产本金及可选退回来源。当前可退本金接口是只读快照，不能代替永久v2原回执；历史详情逐项核对保存回执，未知提交只重试原键/原正文。无退款时旧付款读响应不变，有退款时仅可再分摊余额扣除已退款本金；净已应用量仍单列。规则见[ADR0044](decisions/0044-business-refunds.md)。

账本下的 `POST business-refunds/{refund_id}/corrections` 与 `/cancellations` 使用同一会话、Origin、CSRF 与永久账本幂等键。调用者提交当前 `expected_version` 和修订原因，更正还须完整给出新现金事实、独立费用及有序分摊退回证据。更正沿用原付款，取消完整冲销当前凭证。固定 200 同时保存前版事实、冲销凭证和可选替代事实。提交结果不明时使用原路径、原键、原正文重试，后续更正、取消或关闭期间后仍可读取当版回执。原创建 201 单独保留。

同一退款下的 `GET current` 返回稳定付款与操作身份、版本、有效状态及修订标识。有效退款带当前凭证和完整事实，金额与有序费用使用各自资产的实际精度。取消后当前事实与凭证为空。原详情继续返回固定创建 201，实际退款本金见付款的 refund-capacity。

同一退款下的 `GET history` 按操作版本从 1 开始升序分页。每项含版本、动作、原因、时间与可空修订 ID。目录中选定的 `GET revisions/{revision_id}` 在账本与退款范围内核对该版封存凭证、实际资产精度和存储回执，返回原 200。

## 经营草稿

- 经营草稿使用 `/api/v1/ledgers/{ledger_id}/business-documents`：创建/列表、按ID读取、PUT完整保存和PATCH归档状态；具体字段由OpenAPI生成。均使用现有会话与CSRF边界，不产生分录或财务回执。
- 创建使用稳定整单/行UUID；完整保存要求父expected_version及显式lines，移除用归档，保留行身份/序号；资料快照仅由服务器生成，更换引用或显式刷新才更新。未知提交保留原意图，不自动换ID/版本。来源：[ADR0029](decisions/0029-draft-create-read.md)、[ADR0030](decisions/0030-draft-edit.md)。
- 草稿quantity、unit_price、discount_amount、tax_rate_percent是原始输入回显，保留其合法十进制拼写（含小数尾零、金额负零）；它们不适用上文金额输出规范化规则。派生净额、税额和合计仍按资产精度固定小数位，服务器按方案A重算。
- 草稿接口只服务draft记录；列表默认包含归档，按创建时间/ID升序分页，详情包含有效行。归档草稿可读，显式恢复后才可编辑；确认使用下述独立命令。

## 经营单据确认

- `POST /api/v1/ledgers/{ledger_id}/business-documents/{document_id}/confirm`携带稳定确认UUID、expected_version及Idempotency-Key；客户端不能提交派生金额。按[ADR0039](decisions/0039-business-confirmation.md)原子确认全部有效行，成功201返回完整有序行/操作/凭证映射，旧五类财务接口不包含经营应计。
- 回执使用永久账本键及v2原始JSON摘要；未知提交必须重放原键、原正文及源版本。原回执先于归档、停用、当前版本和期间检查；同键异意图409，已确认单据使用新键也返回409，不产生第二组应计。
- `GET /business-documents/{document_id}/confirmation`在同一账本前缀下返回封存详情与原回执；`GET /business-confirmations`按确认时间/确认ID升序分页，默认100、最大200。列表批量取合计/资产，不加载整单快照；详情保留原输入及税额，读取不刷新资料快照。
- 两类读取都保持账本归属、会话与no-store保护；已归档主体和停用资产的历史仍可读。接口不暴露内部快照JSON，确认、结算和原现金流水不能重复计入管理收支。

整单更正和取消沿用同一会话、Origin/CSRF、账本幂等键及原JSON v2摘要边界。更正提交完整替代表头与1–200行；返回固定200，分别保留原版、替代版及冲销凭证。取消终止单据；有效占用须先显式修订相关业务，不自动解绑或改写资金。原确认及既有回执不变，归档/关闭后仍按原意图重放。读取任何版本不能证明未知POST的结果。来源：[ADR0050](decisions/0050-confirmed-document-revisions.md)。

## 经营收付款与分摊

- 分摊退回使用独立`POST /business-allocation-returns`和v2原意图；明确原分摊行、版本、日期、原因及两侧数量，不移动资金或改费用。`GET /business-allocations/{id}/return-capacity`提供同快照原数量/累计退回/可退数量及当前版本；只供创建新意图，不能证明旧提交成功。退回列表支持payment_id与默认100/最多200分页，按创建时间/ID升序；详情附原完整回执。容量/摘要按资产精度输出，退回原回执数量保留冻结数据库的18位字符串，不改写历史；客户端须按精确数值核对。所有读取保留归属、只读快照及no-store，原付款/分摊详情和回执不变。来源：[ADR0043](decisions/0043-allocation-returns.md)。

- 分摊退回整组修订使用 `POST /business-allocation-returns/{return_id}/corrections` 或 `/cancellations`，携带原组预期版本、修订原因和账本内幂等键。更正提供替代日期、说明及1–200条有序来源和两侧精确金额。200回执永久保留前版、完整冲销、替代及移除成员。任何有效退款仍引用该组时取消返回409，须先显式更正每笔退款的当前证据；更正退回日不能晚于引用它的退款。已确认的原201和各版200均可按原键与原JSON重放。见 [ADR0049](decisions/0049-allocation-return-revisions.md)。

- 退回 `GET /business-allocation-returns/{return_id}/current` 读取同一授权快照中的当前组版本、原付款/往来及至多200条有序有效成员。两侧数量各按其资产精度输出，保留来源版本、永久来源序位和当前凭证；取消后返回终态、空成员及空事实。原详情和回执仍保留创建当时的成员。

- 退回组的 `/history` 从创建版起按组版本升序提供元数据，默认100、上限200，offset上限100000。`/revisions/{revision_id}` 在同账本、同组内返回该版本原200，含所有冲销和可空替代；归档、关闭期间或后续修订均保留其内容。

- 同一账本前缀下，`POST /business-payments`原子创建实际收付款、独立费用及可空初始分摊；`POST /business-allocations`向既有付款追加明确源行应用。均要求原始JSON和永久Idempotency-Key，成功及重放返回201完整组回执；规则见[ADR0040](decisions/0040-business-settlement.md)。
- 同币两种数量相等，跨币分别保存明确实际数量，不用行情推导。累计应用不超过付款本金或源行含税额；应用日不早于付款及各单据开票日，并检查开放期间。历史往来/项目/类别归档不改变封存维度；已有原回执先于当前可用性、容量和期间检查重放。
- `GET /business-payments`与`GET /business-allocations`按创建时间/ID升序分页（默认100，最大200）；后者可按payment_id过滤。`/{id}`详情附原回执，初始分摊附父付款完整回执，后续分摊附独立回执。付款已用/未用为只读派生值，不以不可变行版本冒充余额版本。
- `GET /business-documents/{document_id}/settlement`读取已确认单据逐行及整单原币已结/未结额，包含零源行；草稿不提供结算余量。所有读取使用同一只读快照、账本归属、会话与no-store，保留归档/停用历史；既有确认及普通财务接口不改变。未知提交不能凭当前余量认定成功，须重试原意图并验证完整原回执。

已有付款的`/business-payments/{payment_id}/evidence-target`只读选择目标，单一快照返回完整原付款详情/初始分摊回执及当前操作版本/状态；保留归档历史和账本归属、会话、no-store，不读私有目录或取得写锁。既有付款GET/原回执不变；读取目标不代表本次OCR确认已成功。关联决定见[ADR0042](decisions/0042-payment-evidence-confirmation.md)。

OCR确认增加明确`entry.kind="link_payment"`，选择已有付款和操作版本，只添加同内容原件证据；新action分支封存完整原付款回执/操作版本，旧create/link响应不补新字段。新请求仍逐项复核全部OCR字段并检查版本/有效状态；永久重放只比较不可变原付款、初始分摊与原回执，不依赖当前余额、归档、关闭期间或当前操作状态。未知结果重放原intent_id/完整正文；付款ID与操作ID分别校验，关联不新增资金、费用或分摊。字段以生成OpenAPI为准。

## 独立贷项

`POST /api/v1/ledgers/{ledger_id}/business-credits`按ADR0046接受明确原单据版本、日期及有序原行净额/税额，沿用Cookie/Origin/CSRF、永久账本键及原JSON的v2摘要。成功201为完整不可变组回执；不自动退款、退回分摊或改写原确认。未知提交重试原键和完整正文，归档/停用/期间关闭后仍重放原结果。

同路径GET按`document_id`可选筛选，`limit`默认100、1–200，`offset`默认0、0–100000；稳定按created_at/id排序。`/{credit_id}`详情返回完整原回执。`/business-documents/{document_id}/credit-capacity`以同样分页提供原净/税/含税、累计贷净/税、净分摊、分别剩余净/税及可贷含税量；仅列已确认单据的正金额行，所有分页均在同一只读快照内，当前容量不代表旧提交成功。历史含归档资料，所有接口no-store且同账本鉴权；已有API和回执不补字段。服务层列表每页4次SELECT，非空容量页6次SELECT（含管理员存在性校验，不含HTTP会话验证；由集成测试验证），不随行数逐行查询。

`POST /business-credits/{credit_id}/corrections`提交前一组expected_version、修订reason及完整非空replacement；`/cancellations`提交前一组版本和原因，整组终态取消。按[ADR0047](decisions/0047-credit-group-revisions.md)保留原组、全部旧回执及成员操作历史，新增成员使用新操作v1；普通财务更正、取消和历史入口仍不接受这些子操作。两命令均沿用会话、Origin/CSRF、账本键和绑定路径组ID的原JSON v2，成功及重放返回完整固定200（含全部前后成员及凭证映射），不自动退款或改写原201。

原键优先于当前组版本、取消终态、归档、停用及期间检查；未知结果只能重试原键/原正文，当前容量或原详情不能证明本次修订成功。新意图检查全部原日期及替代日期，期间关闭需先显式重开。现有`/{credit_id}`仍返回不可变创建详情，不代表当前修订状态；接口字段以生成OpenAPI为准，全部响应no-store。

`GET /business-credits/{credit_id}/current`返回稳定组身份、当前组`version`、状态、最新修订身份/原因和最多200个完整有效成员，不分页。当前日期/事实说明与修订原因分开；取消保留终态版本，当前日期/说明为null、成员为空，不以零金额代替已移除成员。当前数量按实际资产精度输出，原201/200回执不变。此接口使用同一只读快照，归档、停用和结账后仍可读；服务固定5次SELECT（含管理员/账本/主体鉴权），HTTP会话另1次。不读取完整分录历史，不获取写锁；稳定组通知可触发重读，但未知POST仍只能重放原意图。

## 公司 Invoice 配置

`/api/v1/ledgers/{ledger_id}/invoice-profiles` 只管理公司账本的开票配置，不分配号码、发行PDF或改变财务分录。POST以稳定UUID创建（201）；GET分页及`/{profile_id}`读取当前配置；PUT `/{profile_id}`以`expected_version`完整替换展示资料、编号规则及Logo选择，省略的可空字段清空；PATCH `/{profile_id}/archive`显式归档或恢复。重复ID与旧版本分别返回409；不存在或跨账本身份返回404。读含归档，分页/排序沿用主资料约定。写入要求当前公司有效，读取保留归档历史。

Logo为本账本私有图片的ID与已审阅版本配对，新选择须仍为有效的PNG/JPEG/WebP；保持原选择不会随文件标题修改而改绑字节。配置只保存当前版本，正式发行须另行冻结所审阅资料。未知资料提交沿用ADR0025，保留原UUID、正文和版本并显式核对；这些响应不是财务命令永久回执，也不表示已预留发票号码。会话、Origin/CSRF、no-store和生成字段合同沿用通用边界。

`POST /api/v1/ledgers/{ledger_id}/invoice-issuances`：对公司账本当前有效 Invoice 提交已审阅的版本、配置版本、前驱及稳定意图 UUID；要求原 `Idempotency-Key`、会话、Origin 与 CSRF。同一意图成功只返回封存的 201，包含不可变号码、快照摘要、中英两份原件的私有文件 ID、长度和摘要；成功后对原键原正文的重试原样重放。原键绑定其他正文或不同意图冲突（409）。服务在短事务中先预留号码，渲染或连接中断不等于未受理，不能生成新意图、键或号码来重试。渲染资源不可用返回 503；不能据此显示发行成功。

新预留（含重新发行）所用的私有 Logo 超过 8 MiB 时，在预留号码前返回 `422 invoice_logo_size_limit`，原幂等键仍可使用。新预留在写事务外核验所审阅的 Logo。可确定违反解码限制时返回 `422 invoice_logo_invalid`，待更换 Logo 后重新审阅。解码环境不可用仍返回 503。已预留的意图继续使用原键、原请求及原冻结图片。

`GET /api/v1/ledgers/{ledger_id}/invoice-issuances` 按账本内已预留序号倒序列出历史，含待完成和已封存条目。可选 `document_id` 过滤，`limit` 默认50、上限200。返回 `items` 与 `next_before`；下一页把游标作为 `before` 原样传回（不含该序号）。序号及游标为正整数十进制字符串，不能转浮点或按页码计算。条目包含签发时单据版本、当前版本及 current/superseded/cancelled；列表不含快照或文件，不应把它用作原命令成功回执。

`GET /api/v1/ledgers/{ledger_id}/invoice-issuances/{issuance_id}` 只读取当前授权账本该意图的已存状态。pending 没有原件，complete 返回两份；授权访问其他公司账本下不属于该账本的意图返回 404。可用现有私有文件 content 接口下载并校验原件，不在下载时重新渲染。单据更正后以新意图、新号码关联上一条；旧回执与两份原件保留。所有响应 no-store。


## 周期 Invoice 草稿

- `/api/v1/ledgers/{ledger_id}/recurring-invoice-rules`提供规则创建/分页读取、按ID读取、PATCH名称/显式模板刷新、PATCH归档状态；`/{rule_id}/instances`及`/{index}`只读原生成实例。均使用既有账本归属、会话、Origin/CSRF和no-store边界，没有公开生成接口。
- 创建捕获指定源Invoice草稿版本，重复稳定ID返回409；更改名称或刷新模板、暂停/恢复都要求expected_version。起点、周期和IANA时区不可修改；模板不会跟随源草稿自动变化。实例以序号升序分页，原生成输入与可编辑的当前草稿分开读取，两者ID相同。
- 规则默认包含归档，分页/排序沿用普通资料约定。只读next_scheduled_date根据原起点和next_index计算，超出日历范围返回null；它不是已生成证明，也不推进游标。后台每生成一个实例都增加规则版本，旧编辑版本返回409。
- 未知提交保留原ID、正文和版本；创建仅在版本1且原日历/源版本/名称完全匹配时可由读取确认，任何更高版本都须显式处理冲突。更新不能凭相似当前字段推断本次已提交。规则见[ADR0033](decisions/0033-recurring-draft-storage.md)，任务调用与积压处理见[运维](../engineering/operations.md)。

## 幂等与回执

- 财务写命令必须带 `Idempotency-Key`：1–128 个不含空格的可见 ASCII 字符，作用域为账本。同键同请求返回永久保存的原回执；同键不同请求返回 409，失败事务不保存成功回执。来源：[ADR 0005](decisions/0005-atomic-posting-and-receipts.md)、[ADR 0008](decisions/0008-operation-revisions-and-cancellation.md)。
- 旧回执按其 v1 版本使用冻结的验证后字段和默认值；新回执一律使用 v2，摘要包含收到的原始 JSON、动作、固定路由模板及账本 ID，更正/取消还包含路径中的业务 ID。JSON 键顺序和空白不影响摘要；金额字符串拼写与数组顺序保留，`"1.0"` 与 `"1.00"` 不同。来源：[ADR 0014](decisions/0014-versioned-command-hashes.md)。
- 重试使用原完整正文与原键，不从可变表单重建，也不把服务端生成的 ID 补进原先省略 ID 的请求。v2 中省略字段、显式 null 或空数组属于不同正文；v1 继续保留省略可选 ID 与 null 等价、原收支/转账省略费用与 `fees: []` 等价的历史规则。无费用回执不补写 `fees`。来源：[ADR 0014](decisions/0014-versioned-command-hashes.md)、[ADR 0010](decisions/0010-financial-web-and-retry.md)。
- 新增及其重放返回原 201 回执，更正/取消及其重放返回原 200 状态回执。读取接口返回当前状态，可能已比回执更新；收到回执后另读当前状态，不用旧回执覆盖新版本。来源：[ADR 0005](decisions/0005-atomic-posting-and-receipts.md)、[ADR 0008](decisions/0008-operation-revisions-and-cancellation.md)。

## 读取与分页

- 贷项组`/business-credits/{credit_id}/history`返回版本升序的轻量目录（含首版create，revision_id为空），limit默认100、1–200，offset默认0、0–100000；目录不嵌成员或回执。`/business-credits/{credit_id}/revisions/{revision_id}`读取指定修订的完整原200，先核对账本/组归属，再校验存储回执与固定历史事实；不截断成员，不以重建结果掩盖损坏，异常返回503。两者保留归档/关闭后的读取及no-store，在同一只读快照内完成；原GET/201不变，目录与详情均不能替代未知POST的原意图重试。

- 有界列表返回数组，`limit` 为 1–200、默认 100，`offset` 为 0–100000、默认 0。采用各接口规定的稳定排序，历史按版本升序；一页未见不代表记录不存在，这些参数不是增量同步游标。来源：[ADR 0004](decisions/0004-owned-ledger-structure.md)、[ADR 0005](decisions/0005-atomic-posting-and-receipts.md)、[ADR 0011](decisions/0011-financial-revision-web.md)。
- 是否包含归档/停用项由接口参数控制，默认值以 OpenAPI 为准；财务列表默认保留全部状态，可显式筛选 active、cancelled 或 all。文件目录和流水证据列表分别分页，不能用一页目录判定关联原件不存在。来源：[ADR 0008](decisions/0008-operation-revisions-and-cancellation.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。
- 流水列表 `order` 默认 `created_at`，按创建时间、业务 ID 倒序；可选 `transaction_date` 按当前入账凭证的交易日、创建时间、业务 ID 倒序。`from_date` 和 `to_date` 使用严格 `YYYY-MM-DD`，按同一当前凭证的交易日作闭区间筛选；反向区间返回 422。已取消业务仍使用最后一张入账凭证的交易日。网页显式选择交易日排序，不改变 API 的默认响应。
- 变更通知通过 `GET /api/v1/changes` 按所有者过滤，`after` 默认 `"0"`、`limit` 默认 100（1–200），返回 `changes` 和 `next_cursor`。`seq`/游标是精确十进制字符串；非空页只推进至最后交付的序号，空页保留原游标，序号允许回滚空洞。归档/停用、恢复及取消分别通知为 archive、restore、cancel，记录本身保留。来源：[ADR 0015](decisions/0015-ordered-change-log.md)。
- 同步客户端用 `GET /api/v1/changes/start` 获取已提交的 `cursor` 和完整 `witness`，可用 `/changes/at/{seq}` 核对保存证据。首次全量读取可以用这组证据登记起点。`/changes` 的可选 `through` 固定本次包含的上限。
- 处理 `financial_operations` 通知时读取 `/api/v1/ledgers/{ledger_id}/operations/{operation_id}/sync-projection`。响应始终包含已存在操作的身份、版本和状态，普通资金流水额外返回现有 `OperationState`。经营及跨账本业务的该投影字段为 `null`，不存在或不属于该账本的操作返回 404。
- `PUT /api/v1/sync/devices/{device_id}` 使用会话登记设备。保存的起点不可变，默认用零起点和空证据。`/retirement` 保存退役，`/history` 读取原登记和退役证据。设备 UUID 只标识登记项。
- 持久化完整页后 POST `/sync/devices/{device_id}/acknowledgements`（`/api/v1` 下），传入 `after`、`through`、`limit`、前置通知 `prior` 和原页 `received`。服务端核对实际顺序、归属及整页内容。通知证据的时间使用带时区的 RFC 3339 字符串，按 UTC 时点核对。空页保留当前进度，无需提交确认。成功页重放返回原回执。`/acknowledgements/latest` 读取最新页回执，`/checkpoint` 读取当前进度与通知证据，包括尚未确认首个非空页的登记起点。恢复后的证据差异返回 409，设备据此暂停并重新对账。[ADR0062](decisions/0062-device-sync-and-resumable-upload.md)
- 首次同步先取得起始游标，再分页读取全量对象，最后从该游标读取后续通知；允许重复获取同一对象。资产使用完整文本 ID，account_assets 使用 `account_uuid:asset_id` 并要求重读父账户，不能因父版本相同而忽略关联变更。通知用于重读当前记录；设备、离线写入和冲突处理由后续完整同步协议定义。来源：[ADR 0015](decisions/0015-ordered-change-log.md)。

## 错误与客户端处理

- 领域错误使用 `{"detail":{"code":"…","message":"…"}}`，客户端按稳定 `code` 和 HTTP 状态判断；消息不回显私人输入、SQL、连接信息或文件路径。未知 HTTP 错误不能凭文本推断已经提交或取消。来源：[ADR 0004](decisions/0004-owned-ledger-structure.md)、[ADR 0009](decisions/0009-business-web-workspace.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。
- 请求校验 422 的 `detail` 是仅含 `loc`、`type`、通用 `msg` 的错误列表；会话错误及通用异常的 `detail` 也可能是字符串，不假定所有错误都有业务 `code`。客户端只保留字段位置、类型及通用消息，不复制原始输入或校验上下文。来源：[ADR 0002](decisions/0002-administrator-session.md)、[ADR 0004](decisions/0004-owned-ledger-structure.md)、[实际错误边界](../../services/api/src/coinpup_api/main.py)。
- 401 要求重新登录，403 表示来源/CSRF 或访问保护失败，409 表示版本或内容冲突；网络中断、超时、服务器错误及无法验证的成功响应均可能留下未知写结果，不能自动换键或换上传 ID。来源：[ADR 0002](decisions/0002-administrator-session.md)、[ADR 0010](decisions/0010-financial-web-and-retry.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。

## 私有原件协议

- 两步上传：先在账本内预留稳定上传 UUID、原文件名、长度和可选流水引用，再以 `application/octet-stream` PUT 完整原件；写鉴权先于读流。同预留 ID/元数据返回原预留，改变请求冲突；拒绝压缩传输，实际字节长度也要符合预留与限额。来源：[ADR 0012](decisions/0012-private-files-and-consistent-bundles.md)。
- 限额和支持格式从配置接口读取；PDF/JPEG/PNG/WebP 的格式边界检查不代表完整解析或恶意内容扫描。整文件接口用原上传 ID 重传相同完整原件；已完成上传仍验证字节大小与摘要。来源：[ADR 0012](decisions/0012-private-files-and-consistent-bundles.md)。
- SHA-256 与大小只在同账本内查重；重复提示不是业务重复判断，不返回其他账本身份。上传和证据关联不创建费用，原件与关联可以分别归档，流水取消或更正仍保留证据。来源：[ADR 0012](decisions/0012-private-files-and-consistent-bundles.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。
- 下载只能走鉴权接口，不把私有目录映射为静态网址。服务器以已验证的打开句柄输出 attachment，附 no-store、nosniff 与 sandbox CSP；浏览器核对大小、媒体类型及 SHA-256，再创建并及时释放 ObjectURL。来源：[ADR 0012](decisions/0012-private-files-and-consistent-bundles.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。
- 文件存储位置、上传超时配置及数据库/原件一致 bundle 的操作命令见 [运维说明](../engineering/operations.md)；数据库单独备份不包含原件。上传、识别与财务确认具有分别的生命周期。来源：[ADR 0012](decisions/0012-private-files-and-consistent-bundles.md)。
- 续传沿用两步上传的预留 UUID。PUT `/api/v1/ledgers/{ledger_id}/uploads/{upload_id}/resumable` 提交原件 `sha256`，GET 同路径读取 `next_offset`、`max_chunk_bytes` 和文件状态。续传接口携带 `X-Coinpup-Device-Id` 及现有会话。服务端绑定原账本、登记设备和摘要，退役设备仍可核对已提交的原回执。
- PUT `/resumable/chunks/{offset}` 使用原始字节。每段按返回的上限提交，末段提交剩余字节。相同偏移和字节重放原段的接收结果。新增段从 GET 返回的当前 `next_offset` 开始。待完成的字节额度按所有者累计，最多为实例单文件限额的四倍。已经初始化续传的预留可用同设备及同摘要通过原整文件入口提交。
- POST `/resumable/complete` 在核对总长、格式和摘要后封存原件，返回既有上传完成回执。提交完成回执时释放暂存字节并保留每段的大小和摘要。待完成的已提交段与原件一起由通用 bundle 覆盖。协议进度独立于现有文件及关联的业务变更通知。[ADR 0062](decisions/0062-device-sync-and-resumable-upload.md)
- POST `/resumable/abandon` 由拥有该账本的已登录会话确认放弃，无需设备头。它保留最后的已确认偏移与分块回执，释放暂存字节并返回可重放的 `abandoned` 终态。设备退役后仍可放弃；已完成的上传拒绝放弃，其原件和回执继续有效。已由整文件入口完成的旧预留拒绝事后绑定续传设备，原请求可按原样重放。

## 网页未知结果

- OCR 草稿列表仅返回分页元数据，支持任务/状态筛选；详情经会话及账本归属校验后才返回原始识别证据、当前复核字段和冻结策略。读取不写状态、不自动确认或入账；归档后历史仍可读。任务原文与草稿属于私有数据，响应 no-store，不含租约、存储路径或数据库配置。缺少新版原文/策略的历史候选明确返回 null，不伪造证据。接口字段以 OpenAPI 为准。

- OCR 创建冻结所有者、账本、原件和稳定 `intent_id`，同意图返回任务当前视图，改变原件冲突；首次配置永久保留，不能随服务器默认值升级。未配置处理器时仍先核归属及旧意图，新建才返回 `ocr_unavailable`。创建响应丢失可按原意图查询并重发；404 不能证明未创建，不静默换意图。任务 pending 只表示排队，上传或排队不产生费用。来源：[ADR 0019](decisions/0019-local-ocr-worker.md)。
- OCR 显式重试沿用原任务和配置，并要求当前版本；尝试次数不清零。重试响应未知时先查询，不能自动用新版本再次重排。状态查询不暴露租约 token、私有路径或原始全文，草稿与人工确认使用独立协议。来源：[ADR 0019](decisions/0019-local-ocr-worker.md)。

- 财务命令和原件上传分别由应用内存控制器保留一个未解决意图；冻结用户、账本、UUID、键/预留元数据和原正文或 File，不写入浏览器持久存储。重复点击共享请求，待确认时限制新命令和导航；刷新/关闭后先核对服务端列表。来源：[ADR 0010](decisions/0010-financial-web-and-retry.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。
- 新增命令可按客户端 UUID 核对当前业务；404 不能证明原提交失败。修订针对既有 UUID，查询任何版本都不能单独确认本次修订，必须以原 POST/原键取得回执；只有明确 version_conflict 加随后更高当前版本的组合证据才可解除该旧修订意图。来源：[ADR 0010](decisions/0010-financial-web-and-retry.md)、[ADR 0011](decisions/0011-financial-revision-web.md)。
- 上传只有相同冻结预留、ready 状态和匹配回执能经查询确认；pending、404、身份不符或无效响应保留待确认。已知字节冲突跨重新登录保留，不能以同文件名/大小误认成功；停止只中断浏览器等待，不能表示服务端取消。来源：[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。
- 未知财务结果保留原命令重试，未知上传结果保留原上传 ID 和完整 File 重试；401 仅隐藏内容，同用户登录后手动继续，异用户/明确退出释放。刷新或关闭丢失内存意图，不具有持久离线队列语义。来源：[ADR 0010](decisions/0010-financial-web-and-retry.md)、[ADR 0011](decisions/0011-financial-revision-web.md)、[ADR 0013](decisions/0013-document-web-and-upload-recovery.md)。


## 提醒事项与修订

- `/api/v1/ledgers/{ledger_id}/reminders`提供事项创建/分页读取；`/rules`返回版本化规则及依据，`/{event_id}`读取/PATCH标题说明，`/revisions`分页读取不可变修订。列表支持include_archived/include_completed，默认均包含；limit 1–200、offset 0–100000。
- `/{event_id}/recalculate` POST显式规则ID/版本和必要参数，主体注册资料由服务器读取；`/manual-date` PATCH设置日期或显式null清除，均要求理由；`/transition` POST指定complete/reopen/archive/restore。所有修改携带原expected_version，归档事项须先恢复；重复ID/过期版本返回409，不自动换ID或版本。
- 计算依据、推算日期与人工覆盖分别保存；effective_date优先人工值，其次仅采用calculated状态日期。读取不重算，重算不清除人工覆盖或完成状态。创建可以不选规则以保存手动税务等事项；未知规则版本/事项类型不匹配为422，待核验/缺资料/不适用有独立状态。
- 全部接口沿用会话归属、Origin/CSRF与no-store。事项和修订保留历史，提醒操作不会生成财务分录。来源：[ADR0035](decisions/0035-reminder-rule-evaluation.md)、[ADR0036](decisions/0036-reminder-event-history.md)。

- `/{event_id}/delivery-config` 读取当前配置、已保存的日期计划和只读预览。PUT 需要同时提交事项与配置版本，配置尚不存在时使用版本 `0`。保存以明确的时区和当地分钟计算的 UTC 时点。[ADR0038](decisions/0038-reminder-delivery.md)
- `/{event_id}/email-deliveries` 按计划时点倒序返回邮件现状。每个事项、渠道及计划时点只有一个邮件标识。`/email-deliveries/{delivery_id}/decision` 需要当前投递版本。明确失败可请求重试，结果未知可记录核实收到或停止投递的理由。邮件服务器接受仅表示 SMTP 接收确认。[ADR0038](decisions/0038-reminder-delivery.md)
- `/inbox` 在账本内按计划时点倒序分页，支持 `unread_only`。通知保留原事项标题、类型、到期日、日期依据、计划与实际投递时点、语言和事项／配置修订身份。PUT `/inbox/{notification_id}/read` 返回同一条通知及其首个已读时间。重复调用返回同一已读事实。已读、事项完成和邮件结果各自独立。

## 可选期间关闭

`GET /ledgers/{ledger_id}/period` 默认开放、版本1；只有显式关闭才产生状态。可同时提供 `from_date`/`to_date` 与 `date_basis`（transaction/recognition），返回所选区间 open/closed/partial、生成时间及与区间相交的最近重开审计；未选区间时 `range_status` 为空。此为T08的状态基础，不生成或保存报表快照。

`POST /ledgers/{ledger_id}/period-changes` 使用独立账本作用域 Idempotency-Key 和原JSON v2摘要，提交 action（close/reopen）、必填可空 closed_through、expected_version、非空 reason。重开必须显式缩小截止日或解除关闭；原键优先重放，版本过期为409 version_conflict。未知提交保留原键与原JSON，不能刷新版本后悄悄重试。`GET` 同路径按版本倒序分页读取不可变审计（limit 1–200，offset 0–100000）。详见ADR0017/0023。

新财务命令任一日期在已关闭范围返回409 period_closed；更正检查原凭证与替代凭证，取消检查原凭证。旧成功回执仍永久重放。未启用结账的账本保持原行为。


## 项目经营读取

`GET /api/v1/ledgers/{ledger_id}/business-projects/{project_id}/activity` 要求登录，必填 `start`、`end`（含首尾的 YYYY-MM-DD）；归档项目可读。按资产身份排序、分页（limit 1–200，默认50；offset 0–100000）。income/cost/net 为期间归属额，closing_receivable/closing_payable 为截至结束日的单据控制余额。金额均按原资产精度返回带符号的字符串；不做跨币种净额。范围逆序或总额超出契约返回422。见[ADR0052](decisions/0052-project-activity.md)。

项目活动另含 attributed_received/attributed_paid：报告期间原付款日、截至结束日明确归属于项目的未退回应用本金，以付款资产列示；不包含未分摊的预付款或未指定项目的费用。见[ADR0053](decisions/0053-attributed-project-payments.md)。

## 手动价格与汇总

- `GET /quotes/manual` 按明确的 `price_date`、展示币及可选资产筛选。每位所有者的资产、展示币、日期组合唯一。价格是正的十进制字符串，最多 20 位整数和 18 位小数。来源为 1–128 个可打印字符，首尾无空白。
- `PUT /quotes/manual` 用 `expected_version: 0` 新建，编辑提交当前版本。相同内容的当前或上一版本请求返回已保存内容，旧版本更改返回 409。价格和来源的每次变更都保留不可变审计，通过相同价格 ID 与版本追溯。本条接口沿用会话、Origin、CSRF 与私有响应边界。
- `GET /reports/mvp` 需要显式选择 1–100 个不同的自有账本。余额按交易日截止，现金按交易日，经营按会计确认日。合计覆盖选择范围内全部明细，分页只影响明细。现金保留单边偿还，双方选中时以已验证的偿还关联排除两边，同时保留原始外部付款。经营只统计收入和费用分录。
- 同时提供 `display_asset_id` 与 `price_date` 使用指定日的直接手动价格。按原币保留明细与合计，每条估值行带上所用价格。缺价时展示币总计为空并列出缺价资产。汇总与估值均为只读。

## 设备财务推送

- `PUT /sync/devices/{device_id}/financial-pushes/{intent_id}` 提交自有账本、原账本幂等键、已确认检查点及一个白名单命令。新建流水必须携带稳定 `payload.id`。更正及取消携带流水 `operation_id` 和原预期版本。旧命令的金额格式、证据、作用域及版本规则继续生效。
- 推送使用原嵌套 JSON 和原账本幂等键。原推送在核对所有者及账本后原样返回，允许检查点推进后及设备退役后重放。修改同一意图的原请求返回 409。新推送必须匹配已确认检查点及完整通知证据。
- 成功应用返回 `outcome=applied` 和原财务回执。发现真正的旧版本冲突时返回 `outcome=conflict`，保留原命令 JSON、依据版本和固定竞争版本。冲突作为独立的同步结果提交。客户端可按原意图核对结果。保留当前版本时提交 `expected_current_version`，记录用户看到的版本；应用离线意图时使用新的意图及账本幂等键，显式提交基于最新版本的原财务命令。
- 同一数据库事务提交财务分录、账本回执和设备意图结果。取消为永久终态，尝试继续修订或重新使用同一流水 ID 会返回冲突。设备意图和用户决定随通用 bundle 保存，并独立于业务通知序列。见 [ADR 0062](decisions/0062-device-sync-and-resumable-upload.md)。

## 自动报价和移动端缓存

- `GET /quotes/effective` 接受 1–100 个不同的 `asset_ids`、一个 `display_asset_id` 和明确的 `price_date`。按请求资产顺序返回 `identity`、`manual`、`automatic` 或 `missing`。手动和自动证据分别返回价格字符串、来源及时间。只有精确时点的提供方填写 `observed_at`，`fetched_at` 保留首次获取时间。
- 原币报表和原有指定日期保持可用。指定日期的手动价格优先，之后查同日自动报价。可用 `price_mode=historical` 取收支交易日或余额截至日，或用 `price_mode=latest` 取每个币对最近的缓存日。自动证据出现在明细的 `automatic_price`，手动证据继续出现在 `price`。当有多行采用不同日期时，汇总的 `price_date` 为空，明细保留各自日期。
- 读取使用已保存的所有者快照。无报价或同一时点有不同自动报价时，返回缺价，原币照常可查。同日手动修正可为该币对提供明确的估值依据。见 [ADR 0055](decisions/0055-read-only-valuations.md)。
