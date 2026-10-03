# 当前项目状态

最后更新：2026-10-03（Asia/Shanghai）。只记录实际交付；验收条件和依赖见 [路线图](roadmap.md)。

## 当前工作

- 仓库：[ygandwx/Coinpup](https://github.com/ygandwx/Coinpup)，保持私有，未选定开源许可证。
- 当前任务：**T02 账务核心及 T04-1 结构网页已集成；T04-2 财务录入与原币流水正在验收**。
- 当前增量分支：`feat/t04-posting-web`，基于已合入 PR #20 的 `98e55a788c7cd4b9999fb161f6c7542afd69ffa9`。
- 用户已授权按路线图持续推进；常规实现、测试、评审和合入无需每个增量再次确认。只有无法自主解决的决策或外部条件才请求输入。
- 已有期初、拆分收支、余额、重试、转账/还款、换汇与手续费接口；更正/取消和完整版本历史已集成。本增量接入五类财务表单、拆分与费用、资产目录和原币流水；网页更正、取消与历史交互留给 T04-3。

## 任务状态

| 阶段 | 任务 | 状态 | 已实现/剩余 |
| --- | --- | --- | --- |
| 1 | T01 工程基础 | 已完成 | PR #13 已合入，Issue #1 已关闭 |
| 1 | T02 账务核心 | 已完成 | T02-1 至 T02-6 均合入；精确金额、独立归属、全部过账、更正/取消与恢复后端验收通过 |
| 1 | T03 API 与数据契约 | 进行中 | 结构/财务 API、稳定 ID、幂等、版本和取消标记已集成；附件契约随 T05，完整增量同步随 T11 |
| 1 | T04 基础网页 | 进行中 | T04-1 已集成；T04-2 财务表单/资产配置/流水正在验收，T04-3 更正/取消/历史接续实现 |
| 2 | T05 本地票据处理 | 待开始 | PDF/照片、本地识别和人工确认 |
| 2 | T06 经营单据 | 待开始 | 客户供应商、项目、应收应付、手动/周期 Invoice |
| 2 | T07 代付与报销 | 待开始 | 可选开启，主体间往来和结算 |
| 3 | T08 汇总与估值 | 待开始 | 原币保留、主体筛选和历史/最新估值 |
| 3 | T09 提醒系统 | 待开始 | 地区规则需逐项核验，含人工覆盖 |
| 3 | T10 部署与运维完善 | 待开始 | 完整财务/附件恢复、升级回滚与自有服务器部署 |
| 4 | T11 同步服务 | 待开始 | 离线重试、冲突和删除传播 |
| 4 | T12 Android / iOS | 待开始 | Flutter、离线与两平台实际验证 |

## 已集成基线

- [PR #13](https://github.com/ygandwx/Coinpup/pull/13)：工程规范、需求、架构、交接、FastAPI/React/PostgreSQL、管理员初始化/会话、双语登录和数据库备份恢复。[CI 37117602098](https://github.com/ygandwx/Coinpup/actions/runs/37117602098) 四项成功，46 项 Linux 单元、5 项真实 PostgreSQL、8 项浏览器检查通过。
- [PR #14](https://github.com/ygandwx/Coinpup/pull/14)：T02-1 精确资产数量与数值绑定边界。[最终 CI 37118851324](https://github.com/ygandwx/Coinpup/actions/runs/37118851324) 四项成功，145 项 Linux 单元、18 项 PostgreSQL、8 项浏览器检查及数据库恢复通过。设计见 [ADR 0003](../architecture/decisions/0003-exact-asset-amounts.md)。
- [PR #15](https://github.com/ygandwx/Coinpup/pull/15)：T02-2 独立主体/账本/账户/分类、结构 API、版本与归档。[最终 CI 37125126788](https://github.com/ygandwx/Coinpup/actions/runs/37125126788) 四项成功；代码 CI 37124617417 的 233 项 Linux 单元、56 项 PostgreSQL、8 项浏览器检查及全部应用表备份恢复通过。设计见 [ADR 0004](../architecture/decisions/0004-owned-ledger-structure.md)。
- [PR #16](https://github.com/ygandwx/Coinpup/pull/16)：T02-3 原子期初/拆分收支/余额与持久幂等。[最终 CI 37126778696](https://github.com/ygandwx/Coinpup/actions/runs/37126778696) 四项成功；代码 CI 37126503279 的 306 项 Linux 单元、120 项 PostgreSQL、8 项浏览器及财务恢复通过。A-03 后端 75 USD/90 EUR、A-07 精度子集和封存验证通过。设计见 [ADR 0005](../architecture/decisions/0005-atomic-posting-and-receipts.md)。
- [PR #17](https://github.com/ygandwx/Coinpup/pull/17)：T02-4 同资产转账与信用卡还款，已合入 `a386264d9599e316e08c04266f96652db41a866e`。[最终 CI 37127694245](https://github.com/ygandwx/Coinpup/actions/runs/37127694245) 四项成功，331 项 Linux 单元、154 项 PostgreSQL、8 项浏览器及财务备份恢复通过。A-04/A-06 后端余额、费用只计一次、并发重试及原转账恢复后重放已验证。设计见 [ADR 0006](../architecture/decisions/0006-same-asset-transfers.md)。
- [PR #18](https://github.com/ygandwx/Coinpup/pull/18)：T02-5 实际换汇与独立手续费，已合入 `7408b421694f7be58e2c3e98e45a791b6fb67bef`。[最终 CI 37128824215](https://github.com/ygandwx/Coinpup/actions/runs/37128824215) 四项成功；代码 [CI 37128573136](https://github.com/ygandwx/Coinpup/actions/runs/37128573136) 的 359 项 Linux 单元、208 项 PostgreSQL、8 项浏览器及全部应用表恢复通过。A-05 的 898 USD/90 EUR、A-07 的 BTC 0.89999000、旧摘要/回执兼容及费用封存已验证。设计见 [ADR 0007](../architecture/decisions/0007-exchanges-and-explicit-fees.md)。
- [PR #19](https://github.com/ygandwx/Coinpup/pull/19)：T02-6 精确更正/取消、连续版本链、当前状态与历史回执分离，已合入 `cf3ab6976356827a0309fc300ba88218bbbb4395`。[最终 CI 37130086947](https://github.com/ygandwx/Coinpup/actions/runs/37130086947) 四项成功；代码 CI 37129766608 的 428 项 Linux、251 项 PostgreSQL、8 项浏览器和完整财务历史恢复通过。A-10 后端及旧回执重放已验证。设计见 [ADR 0008](../architecture/decisions/0008-operation-revisions-and-cancellation.md)。
- [PR #20](https://github.com/ygandwx/Coinpup/pull/20)：T04-1 中英结构工作区、四地公司资料、多资产账户、分类与原币余额，已合入 `98e55a788c7cd4b9999fb161f6c7542afd69ffa9`。[最终 CI 37132297199](https://github.com/ygandwx/Coinpup/actions/runs/37132297199) 四项成功；代码 CI 37131891390 的 428 项 Linux、251 项 PostgreSQL、10 项 Node、16 项浏览器及完整数据库恢复通过。另有 22 张明确虚构数据的桌面/手机视觉检查。设计见 [ADR 0009](../architecture/decisions/0009-business-web-workspace.md)。
- 需求和验收基线是目标；底层检查通过不代表完整业务案例或生产部署完成。

## T04-2 当前增量

设计见 [ADR 0010](../architecture/decisions/0010-financial-web-and-retry.md)。

- 原币期初、拆分收入/支出、转账/信用卡还款、实际换汇与独立手续费表单，金额使用字符串与 BigInt，未将不同资产相加。
- 流水按当前状态读取，保留原币、账户、分类、日期、费用与取消状态；分页及状态筛选。
- 资产目录显式配置法币/加密资产，稳定币保留网络、标识和精度；启停保留历史，新增定义后账户需单独关联。
- 首次有效提交冻结用户、账本、业务 UUID、幂等键及原始正文。未知结果保留，重试不重新生成意图；核对 404 不视为已失败。
- 401 后隐藏业务内容，同一用户重新登录后手动重试；不同用户和主动退出清除状态，迟到响应不会覆盖新命令。控制器仅驻留内存，刷新会丢失待确认内容，界面有提示。
- 新增 11 项控制器/API 单元与 10 项真实浏览器检查；包括后端实际提交成功后丢弃响应、真实会话过期和原键恢复。
- 更正/取消按钮与分录历史在 T04-3 接续；票据、汇率和聚合汇总仍按原路线图实施。

## 当前验证证据与限制

本地 Windows / Node 24：

| 检查 | 实际结果 |
| --- | --- |
| `npm --prefix apps/web run typecheck` | 通过 |
| `npm --prefix apps/web run build -- --configLoader native` | 通过；沙箱使用 native 配置加载 |
| `npm --prefix apps/web run test:unit` | 21 项通过，涵盖精确数量、请求安全和待确认命令生命周期 |
| Playwright 收集 | 26 项；本增量真实运行待 CI |
| 本地视觉预览 | 财务表单与流水检查中；使用虚构数据，不替代真实服务验证 |

T04-2 尚无真实浏览器执行通过记录，正在独立审查和验证。已集成 PR #20 的证据见上，本增量 CI 继续完整回归。没有部署到用户服务器，也未完成附件恢复或阶段 1 整体验收。

## 可执行下一步

1. 完成 T04-2 独立审查、桌面/手机视觉和真实 CI，记录证据后集成。
2. T04-3 接入更正、取消、分录历史和版本冲突，完整验收阶段 1。
3. 随后按路线图推进票据、经营、报表、提醒及移动端。

服务器系统、域名、邮件和实际公司资料在对应配置时确定，不阻塞当前开发。每个 PR 更新本文与相关契约；从其他位置接续时按 AGENTS.md 核对工作区、分支、PR 和 CI，不能只凭聊天重做。
