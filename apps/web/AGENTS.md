# 网页开发规则

适用于 `apps/web/**`，包括组件、请求封装和网页测试。先读根目录 [AGENTS.md](../../AGENTS.md) 与 [API 通用约定](../../docs/architecture/api-conventions.md)；任务进度只看 [status.md](../../docs/engineering/status.md)。

## 金额与业务数据

- 金额从输入、请求到回执始终保留十进制字符串。金额计算使用 [money.ts](src/money.ts) 的 BigInt 工具：`parseAmount`、`amountFromMinorUnits`、`sumAmounts`；展示使用 `formatAmount`。
- 禁止把金额交给 `Number`、`parseFloat` 或浮点运算，包括预览、合计和显示前转换。资产精度、分页和其他非金额整数不受此禁令影响。
- 按完整资产身份及精度处理数量，不混加不同资产，不用展示时的分组符号或本地化小数分隔符改写 API 值。保留服务端精度错误，不静默截断或舍入。

## 请求与未知结果

- 通过 [api.ts](src/api.ts) 及对应业务请求模块调用同源 API，保留 Cookie、Origin、CSRF 和统一错误处理边界。CSRF token 只随当次请求传入。
- 财务命令使用 [PendingCommandController](src/pending-command.ts)，票据确认使用 [PendingConfirmationController](src/pending-confirmations.ts)，原件上传使用 [pending-upload.ts](src/pending-upload.ts)，结账/重开使用 [PendingPeriodController](src/pending-period.ts)。资料编辑使用 [PendingMasterDataController](src/pending-business.ts) 与 [ADR0025](../../docs/architecture/decisions/0025-master-data-retry.md)。组件不得绕过控制器另建重试路径，或仅凭当前记录存在就自行判定本次修订成功。
- 经营草稿使用 [PendingBusinessDraftController](src/pending-business-drafts.ts) 与 [ADR0031](../../docs/architecture/decisions/0031-business-draft-recovery.md)，冻结整单和资产精度；更新冲突不得凭相似内容判为成功。
- 周期规则使用 [PendingRecurringController](src/pending-recurring.ts) 与 [ADR0034](../../docs/architecture/decisions/0034-recurring-rule-recovery.md)；保留原源版本和日历意图，后台生成推进版本也按显式冲突处理。
- 提交前冻结所有者、账本、业务 ID、幂等键、原请求体；上传还要冻结预约 ID、元数据和原始 File。结果未知时重试原意图，不重新生成键或上传 ID，不取当前可编辑表单替换原请求。
- 保留控制器的防重复点击、导航限制、会话恢复及迟到响应隔离。401 隐藏私人内容，同用户重新登录后手动恢复；更换用户或主动退出清理原意图。具体确认和冲突规则遵守 API 通用约定。
- 原始文件、财务命令和会话凭据不得写入 localStorage、sessionStorage、IndexedDB、Cache Storage 等浏览器存储，也不得写入日志。语言偏好可以使用既有 locale 设置；网页内存意图不等同持久离线队列。
- 下载必须经过鉴权、内容核对和临时 ObjectURL 的释放；不暴露私有目录或把原件作为公共静态资源。

## 双语与布局

- 沿用 [i18n.ts](src/i18n.ts) 的语言约定，界面文本、错误提示和无障碍标签同时提供中文与英文。
- 切换语言只改变显示，不重置草稿、选择项、金额字符串、日期、记录版本或待确认命令；用户填写的名称和说明不是可自动翻译的界面文案。
- 界面变更检查桌面、375px、320px 三种宽度。核对横向溢出、长金额和文件名、表单与弹窗按钮、键盘操作及错误提示，不能只检查桌面截图。

## 验证与工具边界

- 从仓库根目录运行 `python scripts/check.py web`，覆盖 TypeScript 检查、网页单元测试及构建；PR 完成前同时遵守根规则的统一检查要求。涉及提交恢复的修改要覆盖响应丢失、同意图重试、401、用户切换和迟到响应。
- e2e 只连接明确的一次性测试服务，设置该服务的 `BASE_URL`、`E2E_USERNAME`、`E2E_PASSWORD`，使用虚构数据；禁止指向真实管理员或生产账本。初始化方式参考 [CI 配置](../../.github/workflows/ci.yml)，再运行 `npm --prefix apps/web run test:e2e`。没有测试服务时报告限制，不把跳过写成通过。
- **OPT-06 完成后**，提交前要求在本目录运行 `npm run format` 与 `npm run lint`。在该工作包落地前，不假定已有这些脚本或相关配置。
- **OPT-07 完成后**，API 类型只能来自 `src/generated/openapi.ts`，生成文件不得手改。在该工作包落地前，按 [OpenAPI 契约](../../contracts/openapi.json) 核对已有类型；不提前引入占位生成文件或声称生成流程存在。
