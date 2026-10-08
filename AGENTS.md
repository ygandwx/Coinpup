# Coinpup 开发规则

Coinpup 是个人与多家公司独立记账的自托管系统；网页联网使用，后续客户端共用业务 API。

## 开工必读与按需阅读

必读只有本文件和 [当前状态](docs/engineering/status.md)。其余文档按任务读取：

| 任务类型 | 需要读 |
| --- | --- |
| 账务、分录、幂等 | [后端规则](services/api/AGENTS.md)、[API 约定](docs/architecture/api-conventions.md)、[ADR 索引](docs/architecture/decisions/README.md) 中相关决定、[对应业务验收](docs/product/acceptance.md) |
| 网页 | [前端规则](apps/web/AGENTS.md)、[API 约定](docs/architecture/api-conventions.md) |
| 文件、OCR、备份 | [ADR 0012](docs/architecture/decisions/0012-private-files-and-consistent-bundles.md)、[ADR 0013](docs/architecture/decisions/0013-document-web-and-upload-recovery.md)、[运维](docs/engineering/operations.md) |
| 新业务模块 | [需求](docs/product/requirements.md) 对应条目、[架构](docs/architecture/overview.md) 第 3–4 节、[路线图](docs/engineering/roadmap.md) 对应任务 |
| 部署、运维 | [运维](docs/engineering/operations.md) |

修改 `services/api/**`、`tests/**` 或 Python 脚本之前，先读并遵守 `services/api/AGENTS.md`；修改 `apps/web/**` 之前，先读并遵守 `apps/web/AGENTS.md`。

子目录规则需要按上述路由主动读取，不能假定根工作目录自动加载全部子文件。

## 用户指示与事实来源

用户的最新指示记录在 status.md 的“用户最新指示”中，优先于此前持续推进授权；当前会话的新指令又优先于该记录。范围、权限及暂停/恢复均按最新授权执行。

进度与限制只写 status.md；产品、验收、架构、API 约定、单接口字段、运维和设计分别使用文档地图里的唯一入口。命令、结果、测试数量和 CI 证据写 PR 描述；已合并 ADR 正文保留，决定变更新增 ADR。聊天不能作为唯一进度来源。

## 不变量

- 金额用十进制字符串传输，整数最小单位/BigInt 计算，数据库 NUMERIC(38,18)；不得经过二进制浮点。
- 每种资产分别配平，分录封存后禁止 UPDATE/DELETE；更正完整冲销并替代，取消只冲销，版本链连续。
- 财务命令必须有账本作用域的幂等键；成功原回执永久保存并原样重放。
- 所有数据明确归属主体/账本，数据库复合外键拒绝跨账本引用。
- Cookie 会话、Origin 与 CSRF 共同保护访问；私有附件只经鉴权下载。
- 显式执行冻结 SQL 迁移；有历史时拒绝降级，不通过删库规避问题。
- 数据库和原件 bundle 在隔离空目标恢复核对。
- 未知提交保留原命令、幂等键、上传 ID 和 File 重试，不静默创建新意图。

完整账务步骤和测试矩阵见后端规则；界面金额与重试约束见前端规则。

## 日常开发循环

1. 读必读文件，执行 `git status --short --branch`、`git log -5 --oneline`，核对当前远端、相关 PR、CI 和任务依赖。保护在途改动；以实际仓库为准，不凭旧聊天重复实施。
2. 从最新 main 开单包分支；每包全绿并 Squash 合入后再开始下一个，等待 CI 时可准备下一包的只读分析。同时最多一个带迁移的开放 PR；合并前 rebase 到最新 main，确认 Alembic 只有一个 head。`contracts/openapi.json` 和前端类型在 rebase 后重新生成，不手工合并冲突。`status.md` 仅在状态变化时更新。
3. 新业务规则或数据模型先写 ADR；产品决定使用 Draft PR，写清方案、推荐和验收影响，等待用户确认。新 ADR 遵循索引模板。
4. 小步实现：每个 PR 建议最多约 800 行有效改动（生成文件、锁文件、快照不计），最多一个迁移；超出时拆分并说明。一个 PR 围绕一个可评审目标。
5. 推送前本地运行 `python scripts/check.py`，涉及网页加 `python scripts/check.py web`。数据库、容器、真实浏览器登录及备份恢复由 CI 的一次性服务验证；不为 Windows 原生 PostgreSQL/Playwright 建立额外环境。已有本地检查入口可保留，不能跳过或删除测试来通过。
6. PR 描述记录问题、最终行为、范围、取舍、实际命令与结果、未运行项及原因、运行影响和下一步。状态变化时才更新 status.md；范围/验收变化同步需求与路线图，接口/配置/运维变化同步对应来源。
7. 检查暂存差异、敏感数据和交接信息。四项必需 CI 检查必须在每个 PR 出现并通过；ci.yml 必需任务不得加路径过滤或草稿跳过，OCR 继续独立走 ocr.yml。已授权自动合入时 Squash 合并；禁止强推/删除受保护 main，平台计费或权限导致 CI 无法运行则停止报告。
8. 结束或交接时确认需要保留的代码已进入可恢复分支/补丁，status.md 写当前任务、分支/PR、实际范围、限制和最多五个可执行下一步；证据留 PR。未提交文件不会随 Git 迁移，凭据和真实备份通过独立安全渠道管理。

## 完成定义

- 实际实现满足任务验收条件，并通过相关检查和真实场景；未执行项必须说明，不能把计划、占位或 mock 当作可用能力/部署。
- 涉及金额、分录或幂等时追加后端测试矩阵；界面检查中英、桌面和手机，后端检查不能替代界面。
- 数据模型有显式迁移和相应验证，不隐式建表、不删库掩盖升级问题。
- PR 提供验证证据与运行影响，状态变化更新 status.md；子步骤不等于整个 T 任务或阶段完成。
- 文档与代码能独立指导接续：新环境先建立依赖和安全配置，验证失败先区分依赖、配置、服务和代码。若工作仍在开放 PR，取得该分支，而非只下载 main。
- 仅用明确虚构数据；不提交密钥、令牌、密码、真实证件/账单/财务记录或开发机绝对路径。外部调用和部署明确作用目标。
- 状态使用待开始/进行中/待评审/已完成/受阻；受阻描述实际依赖与解除方式。稳定任务编号不因会话更换而重写。

## 分支与提交

业务使用 `feat/<task>-<topic>`、修复使用 `fix/<topic>`、维护使用 `chore/<topic>`、纯重构使用 `refactor/<topic>`；优化包按计划编号命名。提交前缀为 `feat:`、`fix:`、`docs:`、`test:`、`refactor:`、`chore:`。

进行中的优化计划：docs/engineering/optimization-plan.md（执行规则见其第 0 节）
