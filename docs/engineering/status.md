# 当前项目状态

最后更新：2026-10-03（Asia/Shanghai）。只记录实际交付；验收条件和依赖见 [路线图](roadmap.md)。

## 当前工作

- 仓库：[ygandwx/Coinpup](https://github.com/ygandwx/Coinpup)，保持私有，未选定开源许可证。
- 当前任务：**T02-6 更正、取消、版本历史及对应 T03 状态契约已实现，本地最终检查通过，等待 CI**。
- 当前增量分支：`feat/t02-revisions`，基于已合入 PR #18 的 `7408b421694f7be58e2c3e98e45a791b6fb67bef`。
- 用户已授权按路线图持续推进；常规实现、测试、评审和合入无需每个增量再次确认。只有无法自主解决的决策或外部条件才请求输入。
- 已有期初、拆分收支、余额、重试、转账/还款、换汇与手续费接口；本增量增加更正/取消和完整版本历史。T04 业务网页尚未开始，接续已验证的账务契约实现。

## 任务状态

| 阶段 | 任务 | 状态 | 已实现/剩余 |
| --- | --- | --- | --- |
| 1 | T01 工程基础 | 已完成 | PR #13 已合入，Issue #1 已关闭 |
| 1 | T02 账务核心 | 进行中 | T02-1/2/3/4/5 已合入；T02-6 更正/取消/历史等待 CI 与验收 |
| 1 | T03 API 与数据契约 | 进行中 | 结构版本、财务幂等、转账/换汇/手续费已集成；当前状态与修订回执等待 CI；增量同步和删除传播仍属 T11 |
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
- [PR #18](https://github.com/ygandwx/Coinpup/pull/18)：T02-5 实际换汇与独立手续费，已合入 `7408b421694f7be58e2c3e98e45a791b6fb67bef`。[最终 CI 37128824215](https://github.com/ygandwx/Coinpup/actions/runs/37128824215) 四项成功；代码 [CI 37128573136](https://github.com/ygandwx/Coinpup/actions/runs/37128573136) 的 359 项 Linux 单元、208 项 PostgreSQL、8 项浏览器及全部应用表恢复通过。A-05 的 898 USD/90 EUR、A-07 的 BTC 0.89999000、旧摘要/回执兼容及费用封存已验证。设计见 [ADR 0007](../architecture/decisions/0007-exchanges-and-explicit-fees.md)。
- 需求和验收基线是目标；底层检查通过不代表完整业务案例或生产部署完成。

## T02-6 / T03 当前增量

设计见 [ADR 0008](../architecture/decisions/0008-operation-revisions-and-cancellation.md)。

- 在 `/api/v1/ledgers/{ledger_id}/operations/{operation_id}` 下增加 `/corrections`、`/cancellations` 和 `/history`；修改要求 `expected_version`、非空原因及幂等键，更正提交不含新 ID 的完整替代内容。
- 同一业务保留稳定 ID 和种类。更正原子追加完整冲销与替代凭证，取消仅追加冲销并进入终态；本金、分类拆分、换汇对方行和全部费用一起处理。
- GET/列表明确改为 `OperationState`，含当前版本、状态和 `latest_posting`；列表默认包含所有状态并支持筛选。旧新增 POST 的形状、摘要及 201 回执不变，修订 POST 保存自己的 200 状态回执。
- 重放先于版本和归档检查，始终返回当时的回执；新旧版本冲突或修改已取消业务返回 409。主体归档暂停新命令，旧账户/分类/资产停用仍允许精确冲销，有效替代引用单独校验。
- 迁移 `20261003_0007` 增加业务状态与凭证原因，验证完整连续版本链、逐行精确逆向、唯一冲销和最近正常入账指针；历史不可变、凭证继续封存，存在修订历史时拒绝降级。
- 期初更正保留账户/资产身份，取消后占用标记仍保留；取消终态支持后续删除传播，但当前没有设备增量同步服务。
- 恢复夹具增加费用 100→120 且 ETH 手续费更正，以及含 USDC 手续费的 USD/EUR 换汇更正后在账户归档状态下取消；验证当前状态、完整历史、旧创建回执与各次修改回执重放、全部行精度及正常/反向凭证封存。

## 当前验证证据与限制

本地 Windows / Python 3.12.14：

| 检查 | 实际结果 |
| --- | --- |
| 全量 Python 测试 | 427 通过、252 跳过；251 项 PostgreSQL 与 1 项 POSIX 权限等待 CI |
| `python -m ruff check .` | 通过 |
| `python -m ruff format --check .` | 94 个 Python 文件通过 |
| `python scripts/check_docs.py` | 18 个 Markdown 文件的入口与本地链接通过 |
| `python scripts/export_openapi.py --check` | 新增修订接口、状态响应与实际 OpenAPI 快照一致 |
| Alembic 离线 SQL | `20261003_0007` 升级/降级生成通过；不替代真实迁移 |
| T02-6 PostgreSQL、浏览器与真实恢复 | 等待 CI；本增量尚无成功记录 |

独立审查覆盖逐行精确冲销、全部费用、连续版本链、终态保护、最终余额范围、旧创建回执兼容、封存及安全降级。主体归档规则已统一为暂停全部新财务命令、允许既有回执重放。已有读取测试已适配 `OperationState.latest_posting`；恢复审计检查在低精度 Decimal 上下文中保持精确，能拒绝漏费、金额改变、日期改变和版本缺口。真实 PostgreSQL 与恢复场景仍需 CI，不能沿用 PR #18 的成功证据；完整业务网页仍未交付。

本地没有 PostgreSQL/Docker；数据库和容器验收由 GitHub Actions 提供。测试仍有既有 Starlette/httpx 弃用警告。没有部署到用户服务器，没有附件恢复或完整业务验收记录。

## 可执行下一步

1. 完成 T02-6 PostgreSQL/HTTP/备份恢复 CI 与评审，记录证据后合入；按验收结果更新 #2/#3。
2. T04 接入真实网页闭环，包括主体/账户/分类、财务录入、当前状态、版本冲突和历史；该任务尚未开始。
3. 随后按路线图推进票据、经营、报表、提醒及移动端；保留 T11 的同步与旧设备防复活验收。

服务器系统、域名、邮件和实际公司资料在对应配置时确定，不阻塞当前开发。每个 PR 更新本文与相关契约；从其他位置接续时按 AGENTS.md 核对工作区、分支、PR 和 CI，不能只凭聊天重做。
