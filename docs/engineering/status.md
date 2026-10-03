# 当前项目状态

最后更新：2026-10-03（Asia/Shanghai）。只记录实际交付；验收条件和依赖见 [路线图](roadmap.md)。

## 当前工作

- 仓库：[ygandwx/Coinpup](https://github.com/ygandwx/Coinpup)，保持私有，未选定开源许可证。
- 当前任务：**T02-5 实际换汇、独立手续费及对应 T03 契约已实现，本地最终检查通过，等待 CI**。
- 当前增量分支：`feat/t02-exchange-fees`，基于已合入 PR #17 的 `a386264d9599e316e08c04266f96652db41a866e`。
- 用户已授权按路线图持续推进；常规实现、测试、评审和合入无需每个增量再次确认。只有无法自主解决的决策或外部条件才请求输入。
- 已有期初、拆分收支、余额、重试、同资产转账与信用卡还款接口；本增量增加实际换汇与独立手续费。下一步为 T02-6 更正与取消；T04 业务网页尚未开始。

## 任务状态

| 阶段 | 任务 | 状态 | 已实现/剩余 |
| --- | --- | --- | --- |
| 1 | T01 工程基础 | 已完成 | PR #13 已合入，Issue #1 已关闭 |
| 1 | T02 账务核心 | 进行中 | T02-1/2/3/4 已合入；T02-5 换汇/手续费等待 CI；T02-6 更正/取消待实现 |
| 1 | T03 API 与数据契约 | 进行中 | 结构版本/归档、财务幂等与转账联合回执已集成；换汇/手续费兼容契约等待 CI；更正/删除/同步扩展点继续实现 |
| 1 | T04 基础网页 | 待开始 | 当前只有登录入口；需接入实际业务接口 |
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
- 需求和验收基线是目标；底层检查通过不代表完整业务案例或生产部署完成。

## T02-5 / T03 当前增量

设计见 [ADR 0007](../architecture/decisions/0007-exchanges-and-explicit-fees.md)。

- `POST /api/v1/ledgers/{ledger_id}/exchanges` 保存两端不同资产的实际数量，支持同一个多资产账户内换汇；换汇本金不计为收入或费用。
- 收入、支出、转账和换汇支持最多 20 项明确手续费，每项指定同账本扣款账户、资产、正数金额与费用分类；可使用其他账户或第三种资产，期初不支持费用。
- 本金和全部费用在一个事务内验证并提交，按账户/资产合并所有增减后检查最终余额范围；任一费用失败整体回滚。
- 新增 `ExchangeResponse`，流水按 `kind` 返回判别联合类型；旧收支/转账仅有费用时增加 `fees`。四类旧命令摘要保持不变，省略费用与显式空数组兼容，历史回执不改写。
- 迁移 `20261003_0006` 增加 `component_no`：0 为本金，1–20 为独立费用。旧行升级为 0；每部分逐资产配平，保留归属、精度和封存保护。
- 降级遇到已有换汇或手续费历史明确拒绝，不删除或降格解释数据。
- 恢复夹具增加独立账户的 1000 USD→兑换 100 USD/90 EUR、费用 2 USD，以及 1 BTC→支出 0.1 BTC、费用 0.00001 BTC；恢复预期为 898 USD/90 EUR 和 0.89999000 BTC，并验证本金/费用回执的查询、列表、重放及费用行封存。原有转账、还款、归档及 ETH/USDC/JPY 精度场景保留。

## 当前验证证据与限制

本地 Windows / Python 3.12.14：

| 检查 | 实际结果 |
| --- | --- |
| Python 测试 | 358 通过、209 跳过；208 项 PostgreSQL 与 1 项 POSIX 权限等待 CI |
| `python -m ruff check .` | 通过 |
| `python -m ruff format --check .` | 87 个 Python 文件通过 |
| `python scripts/check_docs.py` | 17 个 Markdown 文件的入口与本地链接通过 |
| `python scripts/export_openapi.py --check` | 实际接口与 OpenAPI 快照一致 |
| Alembic 离线 SQL | 升级至 `20261003_0006` 及 `0006:0005` 降级生成通过；不替代真实迁移 |
| T02-5 PostgreSQL、浏览器与真实恢复 | 等待 CI；当前没有本增量的成功记录 |

独立审查覆盖本金/费用独立配平、整笔原子性、最终余额范围、旧摘要与回执 JSON 兼容、封存及安全降级，未发现阻塞问题。固定旧摘要样例、响应序列化和备份校验单测已通过；A-05/A-07 的真实 PostgreSQL、HTTP 与恢复场景已加入检查，实际结果待 CI。PR #17 的成功证据只代表已合入的转账增量，不能替代 T02-5 验收；完整业务网页仍未交付。

本地没有 PostgreSQL/Docker；数据库和容器验收由 GitHub Actions 提供。测试仍有既有 Starlette/httpx 弃用警告。没有部署到用户服务器，没有附件恢复或完整业务验收记录。

## 可执行下一步

1. 完成 T02-5 最终检查与 CI，记录实际执行证据；通过后合入并更新 #2/#3，T02 整体保持开放。
2. T02-6 实现冲销/替代、取消、修改版本与审计；整笔处理本金及全部手续费，保留稳定 ID 和既有幂等回执。
3. T04 接入真实网页闭环；随后按路线图推进票据、经营、报表、提醒及移动端。

服务器系统、域名、邮件和实际公司资料在对应配置时确定，不阻塞当前开发。每个 PR 更新本文与相关契约；从其他位置接续时按 AGENTS.md 核对工作区、分支、PR 和 CI，不能只凭聊天重做。
