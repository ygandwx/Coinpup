# 当前项目状态

最后更新：2026-10-03（Asia/Shanghai）。只记录实际交付；验收条件和依赖见 [路线图](roadmap.md)。

## 当前工作

- 仓库：[ygandwx/Coinpup](https://github.com/ygandwx/Coinpup)，保持私有，未选定开源许可证。
- 当前任务：**T02 账务核心及 T04-1/T04-2 已集成；T04-3 更正、取消和版本历史正在验收**。
- 当前增量分支：`feat/t04-revision-web`，基于已合入 PR #21 的 `3a50b3a7a95fe35ab38aa0c81522ad4ce8014937`。
- 用户已授权按路线图持续推进；常规实现、测试、评审和合入无需每个增量再次确认。只有无法自主解决的决策或外部条件才请求输入。
- 已有期初、拆分收支、余额、重试、转账/还款、换汇与手续费接口；更正/取消和完整版本历史已集成。五类财务录入、资产目录与原币流水已集成；本增量接入网页更正、取消、历史及完整手动流程验收。

## 任务状态

| 阶段 | 任务 | 状态 | 已实现/剩余 |
| --- | --- | --- | --- |
| 1 | T01 工程基础 | 已完成 | PR #13 已合入，Issue #1 已关闭 |
| 1 | T02 账务核心 | 已完成 | T02-1 至 T02-6 均合入；精确金额、独立归属、全部过账、更正/取消与恢复后端验收通过 |
| 1 | T03 API 与数据契约 | 进行中 | 结构/财务 API、稳定 ID、幂等、版本和取消标记已集成；附件契约随 T05，完整增量同步随 T11 |
| 1 | T04 基础网页 | 进行中 | T04-1/T04-2 已集成；T04-3 更正/取消/历史及手动流程正在验收 |
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
- [PR #21](https://github.com/ygandwx/Coinpup/pull/21)：T04-2 五类财务表单、拆分/手续费、资产目录、原币流水和未知提交重试，已合入 `3a50b3a7a95fe35ab38aa0c81522ad4ce8014937`。[最终 CI 37133307266](https://github.com/ygandwx/Coinpup/actions/runs/37133307266) 四项成功；代码 CI 37133043796 的 428 项 Linux、251 项 PostgreSQL、21 项 Node、26 项真实浏览器与完整数据库恢复通过。30 项本地虚构数据交互/视觉验证通过。设计见 [ADR 0010](../architecture/decisions/0010-financial-web-and-retry.md)。
- 需求和验收基线是目标；底层检查通过不代表完整业务案例或生产部署完成。

## T04-3 当前增量

设计见 [ADR 0011](../architecture/decisions/0011-financial-revision-web.md)。

- 流水提供更正、取消和逐版本历史入口；更正预填完整本金、分类、费用与日期，版本冻结，原因必填。
- 旧窗口冲突保留草稿；明确重新载入当前记录才替换版本与输入。已取消记录不能重新激活。
- 修订继续使用原业务 ID 和固定幂等键，原修订回执与当前查询状态分离。查询既有记录不能误认本次修订成功；原请求重放后重新读取当前流水。
- 版本历史展示执行人、原因、入账/冲销凭证和逐资产精确带符号金额，不重算或改写旧记录。
- 取消允许旧账户、资产关联或分类已停用的历史冲销；归档账本仍暂停新命令。
- 新增桌面/手机真实流程：100→120→取消及最小ETH手续费、双窗口版本冲突、旧/新当前版本均不能误确认未知修订、历史回执不覆盖更高取消版本。

## 当前验证证据与限制

本地 Windows / Node 24：TypeScript 检查、生产构建（沙箱采用 `--configLoader native`）、32 项 Node 单元及 21 份 Markdown 链接检查通过。已收集 32 项浏览器测试；真实 CI 和新增界面视觉检查待完成。T04-2 的实际数据库/浏览器证据见上，不能视作本增量已执行。

没有部署到用户服务器。T04 完成只代表联网手动记账闭环；A-01 证件附件、A-08 上传照片、A-09 附件恢复等跨任务部分仍须 T05/T10，不能提前标为整体通过。

## 可执行下一步

1. 完成 T04-3 独立审查、桌面/手机视觉和真实 CI，记录手动流程证据后集成并关闭 T04。
2. 开始 T05 本地文件上传、归属关联、访问保护、备份恢复，再接文字提取/OCR、待确认草稿和确认入账。
3. 按路线图接续经营单据、往来报销、报表、提醒与移动端；接口契约随实际模块更新。

服务器系统、域名、邮件和实际公司资料在对应配置时确定，不阻塞当前开发。每个 PR 更新本文与相关契约；从其他位置接续时按 AGENTS.md 核对工作区、分支、PR 和 CI，不能只凭聊天重做。
