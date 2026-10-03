# 当前项目状态

最后更新：2026-10-04（Asia/Shanghai）。只记录实际交付；验收条件和依赖见 [路线图](roadmap.md)。

## 当前工作

- 仓库：[ygandwx/Coinpup](https://github.com/ygandwx/Coinpup)，保持私有，未选定开源许可证。
- 当前任务：**T05-1 私有票据与一致恢复、T05-2 票据网页均已完成验收；本轮保存后暂停，等待用户指示。**
- 本轮保存点：[PR #24](https://github.com/ygandwx/Coinpup/pull/24)，分支 `feat/t05-documents-web`，基于已合入 PR #23 的 `0fa8c005e16b709c32243187f035d19bb88a1328`。合入后从 `main` 续接，最终提交与合入状态以该 PR 为准。
- 用户最新指示：**完成当前已启动的票据网页环节并保存，然后暂停，等待用户新指示。不得继续启动 OCR 或其他任务。** 此指示优先于此前持续推进授权。
- 已交付完整手动记账、私有原件后端及中英票据目录、上传恢复、下载校验、版本标题和流水证据；上传不产生费用。

## 任务状态

| 阶段 | 任务 | 状态 | 已实现/剩余 |
| --- | --- | --- | --- |
| 1 | T01 工程基础 | 已完成 | PR #13 已合入，Issue #1 已关闭 |
| 1 | T02 账务核心 | 已完成 | T02-1 至 T02-6 均合入；精确金额、独立归属、全部过账、更正/取消与恢复后端验收通过 |
| 1 | T03 API 与数据契约 | 进行中 | 结构/财务 API、稳定 ID、幂等、版本和取消标记已集成；附件契约随 T05，完整增量同步随 T11 |
| 1 | T04 基础网页 | 已完成 | PR #20–#22 已集成；桌面/手机手动流程、修订和历史通过，Issue #4 已关闭 |
| 2 | T05 本地票据处理 | 进行中（已暂停） | T05-1、T05-2 验收完成；本地识别与人工确认尚未开始，等待用户明确继续 |
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
- [PR #22](https://github.com/ygandwx/Coinpup/pull/22)：T04-3 更正、取消、版本历史与完整手动流程，已合入 `c3d00366d0a23e6100f4bb30ceade9207b55520c`。[最终 CI 37134542971](https://github.com/ygandwx/Coinpup/actions/runs/37134542971) 四项成功；代码 CI 37134148056 的 428 项 Linux、251 项 PostgreSQL、32 项 Node、32 项真实浏览器及全部应用表恢复通过。另有 33 个桌面/375px/320px 视觉场景。设计见 [ADR 0011](../architecture/decisions/0011-financial-revision-web.md)。
- [PR #23](https://github.com/ygandwx/Coinpup/pull/23)：T05-1 私有文件、两步上传、不可变回执、同账本查重/证据关联与一致 bundle，已合入 `0fa8c005e16b709c32243187f035d19bb88a1328`。[最终 CI 37137230844](https://github.com/ygandwx/Coinpup/actions/runs/37137230844) 四项成功；代码 CI 37136897576 的 568 项 Linux、286 项 PostgreSQL、32 项 Node、32 项既有真实浏览器与数据库/原件一致恢复通过。设计见 [ADR 0012](../architecture/decisions/0012-private-files-and-consistent-bundles.md)。
- 需求和验收基线是目标；底层检查通过不代表完整业务案例或生产部署完成。

## T05-2 已验收增量与保存点

设计见 [ADR 0013](../architecture/decisions/0013-document-web-and-upload-recovery.md)。

- 中英独立账本票据目录，公司资料页证件入口、流水证据入口，原件上传/下载、标题/归档管理与分页。
- 原 File、稳定 UUID、元数据仅保留在内存，丢响应/停止后保留原上传；相同预留 ready 查询可确认，pending/404 不误判失败。
- 401 隐藏上传内容，同用户登录后手动恢复原上下文；异用户/明确退出释放文件并忽略迟到响应。已知内容冲突跨重新登录仍保留。
- 标题冲突保留草稿与旧版本，显式重新载入才替换。流水证据和原件可分别归档；取消流水仍保留证据，关联不修改账务金额。
- 下载校验返回大小、媒体类型与 SHA-256 后触发浏览器下载，ObjectURL 及时释放；上传确认后有独立下载入口，覆盖复用归档原件及分页场景。
- 添加桌面/手机真实流程，验证 PDF/PNG 字节、查重、账本隔离、版本冲突、证据、登录恢复及提交丢响应后的核对。

## 当前验证证据与限制

代码提交 `d49815b61989e53af9e417cb0ed613218c107e86` 的 [CI 37138353943](https://github.com/ygandwx/Coinpup/actions/runs/37138353943) 四项全部成功：568 项 Linux 单元、286 项真实 PostgreSQL 集成、48 项 Node 单元、40 项真实桌面/手机浏览器检查通过。迁移往返、模型一致性、OpenAPI/文档检查、生产构建及数据库/私有原件一致 bundle 恢复通过。最终交接文档提交的 CI 与合入记录见 PR #24；不得把代码 CI 记录误作生产部署证据。

本地 Windows/Node 24：TypeScript、48 项 Node 单元、生产构建（沙箱用 `--configLoader native`）及 23 份 Markdown 链接检查通过。30 个明确虚构数据的 1440/375/320px 视觉/交互场景通过，包含归档重复原件快捷下载，长文件名、标题、双语冲突草稿与待确认上传均无页面横向溢出。这些 mock 场景仅用于交互和视觉检查，真实持久化验收由上述 CI 承担。

没有部署到用户服务器。T04 完成只代表联网手动记账闭环；T05 已验证证件原件入口、照片上传与当前数据库/原件一致恢复，但 A-01 的到期提醒、A-08 的 OCR/人工确认以及 T10 的全部业务和生产恢复仍待后续任务，不能提前标为整体通过。

## 可执行下一步

1. **当前暂停，等待用户明确要求继续；不自动启动下一增量。** 续接时先确认 PR #24 的合入和 CI 状态，再按 AGENTS.md 检查本地分支与未提交工作。
2. 收到指示后从 T05-3 本地文字提取/OCR worker 开始，随后 T05-4 待确认草稿与人工确认，不重做已交付的上传和手动记账。
3. 经营单据、往来报销、报表、提醒与移动端仍按路线图实施；当前暂停期间不自动启动。

服务器系统、域名、邮件和实际公司资料在对应配置时确定，不阻塞当前开发。每个 PR 更新本文与相关契约；从其他位置接续时按 AGENTS.md 核对工作区、分支、PR 和 CI，不能只凭聊天重做。
