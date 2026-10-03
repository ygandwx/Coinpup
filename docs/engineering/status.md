# 当前项目状态

最后更新：2026-10-03（Asia/Shanghai）。只记录实际交付；验收条件和依赖见 [路线图](roadmap.md)。

## 当前工作

- 仓库：[ygandwx/Coinpup](https://github.com/ygandwx/Coinpup)，保持私有，未选定开源许可证。
- 当前任务：**T02-4 同资产转账、信用卡还款及对应 T03 契约已通过 CI，等待合入**。
- 当前增量分支：`feat/t02-transfers`，基于已合入 PR #16 的 `9c5da9ece04aab2b0806fca65d074f764901895e`。
- 用户已授权按路线图持续推进；常规实现、测试、评审和合入无需每个增量再次确认。只有无法自主解决的决策或外部条件才请求输入。
- 已有期初、拆分收支、余额和重试接口；本增量增加同资产转账与信用卡还款。业务网页和完整更正流程尚未完成。

## 任务状态

| 阶段 | 任务 | 状态 | 已实现/剩余 |
| --- | --- | --- | --- |
| 1 | T01 工程基础 | 已完成 | PR #13 已合入，Issue #1 已关闭 |
| 1 | T02 账务核心 | 进行中 | T02-1/2/3 已合入；T02-4 转账/还款验收中；换汇/手续费/更正待实现 |
| 1 | T03 API 与数据契约 | 进行中 | 结构版本/归档与初始财务幂等已集成；转账联合回执验收中；更正/删除/同步扩展点继续实现 |
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
- 需求和验收基线是目标；底层检查通过不代表完整业务案例或生产部署完成。

## T02-4 / T03 当前增量

设计见 [ADR 0006](../architecture/decisions/0006-same-asset-transfers.md)。

- `POST /api/v1/ledgers/{ledger_id}/transfers` 原子转移同一资产，明确转出/转入两个不同账户，不增加收入/费用。
- 信用卡消费沿用支出命令，还款使用转账；允许负余额表示欠款，还款只清偿余额。
- 双方归属、账户/资产状态及结果余额范围在写入前全部检查；任一侧失败整体回滚，复用持久幂等。
- 新增 `TransferResponse`，流水按 `kind` 返回判别联合类型；旧收支回执字段与请求摘要保持兼容。
- 迁移 `20261003_0005` 扩展操作类型与延迟校验，要求两条等额反向账户行；旧精度、归属、配平和封存约束保留。
- 降级遇到已有转账历史明确拒绝，不删除或改写数据。
- 财务恢复夹具增加银行 1000→转现金 200、信用卡消费及还款 100；恢复应得到银行 700、现金 200、信用卡 0，费用仅 100，并重放原转账回执。

## 当前验证证据与限制

本地 Windows / Python 3.12.14：

| 检查 | 实际结果 |
| --- | --- |
| `python -m pytest --basetemp <新的工作区临时目录>` | 330 通过、155 跳过；154 项 PostgreSQL 与 1 项 POSIX 权限等待 CI |
| `python -m ruff check .` | 通过 |
| `python -m ruff format --check .` | 81 个 Python 文件通过 |
| `python scripts/check_docs.py` | 16 个 Markdown 文件的入口与本地链接通过 |
| `python scripts/export_openapi.py` | 已生成实际财务接口快照；提交前检查一致性 |
| `python -m alembic upgrade head --sql` | 生成迁移升级 SQL 通过；离线降级 SQL 也通过，不替代真实迁移 |

独立审查覆盖两端归属、双边余额、旧回执与摘要兼容、配平/封存及安全降级，未发现阻塞问题。[PR #17](https://github.com/ygandwx/Coinpup/pull/17) 代码提交 `ef7fbd507937d82154dcb7f714c6b768023839f4` 的 [CI 37127376404](https://github.com/ygandwx/Coinpup/actions/runs/37127376404) 四项作业全部成功：331 项 Linux 单元、154 项 PostgreSQL、8 项浏览器测试、迁移往返/模型一致性、网页构建及财务备份恢复通过。A-04/A-06 后端余额、费用只计一次、并发重试及原转账恢复后重放已验证；完整业务网页仍未交付。

本地没有 PostgreSQL/Docker；数据库和容器验收由 GitHub Actions 提供。测试仍有既有 Starlette/httpx 弃用警告。没有部署到用户服务器，没有附件恢复或完整业务验收记录。

## 可执行下一步

1. 合入已验证增量并更新 #2/#3，T02 整体保持开放。
2. T02-5 实现换汇与独立手续费，验证 A-05/A-07；保存实际数量，逐资产配平，保持旧命令幂等合同。
3. T02-6 实现冲销/替代和修改版本。
4. T04 接入真实网页闭环；随后按路线图推进票据、经营、报表、提醒及移动端。

服务器系统、域名、邮件和实际公司资料在对应配置时确定，不阻塞当前开发。每个 PR 更新本文与相关契约；从其他位置接续时按 AGENTS.md 核对工作区、分支、PR 和 CI，不能只凭聊天重做。
