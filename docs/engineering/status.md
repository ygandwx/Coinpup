# 当前项目状态

最后更新：2026-10-03（Asia/Shanghai）。只记录实际交付；验收条件和依赖见 [路线图](roadmap.md)。

## 当前工作

- 仓库：[ygandwx/Coinpup](https://github.com/ygandwx/Coinpup)，保持私有，未选定开源许可证。
- 当前任务：**T02 账务核心已合入并完成后端验收；T04-1 真实结构管理网页正在验收**。
- 当前增量分支：`feat/t04-structure-web`，基于已合入 PR #19 的 `cf3ab6976356827a0309fc300ba88218bbbb4395`。
- 用户已授权按路线图持续推进；常规实现、测试、评审和合入无需每个增量再次确认。只有无法自主解决的决策或外部条件才请求输入。
- 已有期初、拆分收支、余额、重试、转账/还款、换汇与手续费接口；更正/取消和完整版本历史已集成。本增量接入公司资料、独立账户/分类与原币余额页面；网页财务录入和历史操作仍待下个增量。

## 任务状态

| 阶段 | 任务 | 状态 | 已实现/剩余 |
| --- | --- | --- | --- |
| 1 | T01 工程基础 | 已完成 | PR #13 已合入，Issue #1 已关闭 |
| 1 | T02 账务核心 | 已完成 | T02-1 至 T02-6 均合入；精确金额、独立归属、全部过账、更正/取消与恢复后端验收通过 |
| 1 | T03 API 与数据契约 | 进行中 | 结构/财务 API、稳定 ID、幂等、版本和取消标记已集成；附件契约随 T05，完整增量同步随 T11 |
| 1 | T04 基础网页 | 进行中 | T04-1 主体/地区/账户/分类/余额正在验收；财务表单、记录与历史交互接续实现 |
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
- 需求和验收基线是目标；底层检查通过不代表完整业务案例或生产部署完成。

## T04-1 当前增量

设计见 [ADR 0009](../architecture/decisions/0009-business-web-workspace.md)。

- 登录后进入真实账本工作区，桌面侧栏与手机导航均可操作；按主体读取账户、分类和原币余额，刷新恢复所选视图。
- 创建个人/四地公司、复制独立分类模板，编辑地区资料和自定义字段；编号保持字符串、可稍后补齐。
- 管理九类账户和多资产关联、银行/钱包资料；归档恢复保留余额和历史引用。
- 分类支持创建父子关系、编辑中英名称、归档恢复；冲突保留草稿，明确重新载入最新版本。
- 类型化 API 保留状态/业务错误，401 返回登录；CSRF 只在内存。金额工具采用字符串与 BigInt，未将不同资产相加。
- 新增 10 项 Node 测试和 8 项结构浏览器流程（含桌面/手机）；真实 CI 结果待验证。
- 尚未提供网页财务录入、记录/历史操作、资产目录编辑、附件与提醒；这些不因管理页完成而提前标记交付。

## 当前验证证据与限制

本地 Windows / Node 24：

| 检查 | 实际结果 |
| --- | --- |
| `npm --prefix apps/web run typecheck` | 通过 |
| `npm --prefix apps/web run build -- --configLoader native` | 通过；沙箱使用 native 配置加载 |
| `npm --prefix apps/web run test:unit` | 10 项通过，涵盖状态错误、CSRF/请求路径和精确金额 |
| Playwright 收集 | 16 项；真实浏览器与持久化执行等待 CI |
| 本地视觉预览 | 使用明确虚构接口数据；22 张 1440/375/320px 截图检查通过，长名称无横向溢出、金额单行可键盘滚动；不替代真实服务测试 |
| `python scripts/check_docs.py` | 19 份 Markdown 的入口与本地链接通过 |

T04-1 尚无真实浏览器执行通过记录。后端现有 428 项 Linux、251 项数据库与恢复证据属于已集成 PR #19；本增量 CI 会继续回归。没有部署到用户服务器，没有附件恢复或完整阶段 1 用户流程验收记录。

## 可执行下一步

1. 完成 T04-1 独立审查、桌面/手机视觉及真实浏览器 CI，修复问题后合入。
2. T04-2 接入期初/收入/支出/拆分/转账/换汇/费用表单、原币流水与资产配置，保留未知提交结果的幂等重试状态。
3. T04 后续接入更正、取消、历史与版本冲突，完整验收阶段 1；随后按路线图推进票据、经营、报表、提醒及移动端。

服务器系统、域名、邮件和实际公司资料在对应配置时确定，不阻塞当前开发。每个 PR 更新本文与相关契约；从其他位置接续时按 AGENTS.md 核对工作区、分支、PR 和 CI，不能只凭聊天重做。
