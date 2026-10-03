# 当前项目状态

最后更新：2026-10-03（Asia/Shanghai）。本文件记录实际状态；计划与验收条件见 `roadmap.md`。

## 当前工作

- 仓库：[`ygandwx/Coinpup`](https://github.com/ygandwx/Coinpup)，保持私有，未选定开源许可证。
- 当前任务：**T02 账务核心，进行中**；T01 已验收、审查并集成。
- 本轮目标：完成 T02 首个增量——资产身份、精确金额与数据库数值边界；不将此增量当作完整账务核心。
- 本增量分支：`feat/t02-money-foundation`，基于 T01 合并提交 `9c05be65e222389b2abe62ca5e89db98db0f51c5`。后续从已集成的最新 `main` 继续，先核对 PR 状态。
- T01：[PR #13](https://github.com/ygandwx/Coinpup/pull/13) 已合入 `main`，[Issue #1](https://github.com/ygandwx/Coinpup/issues/1) 已完成关闭；[T02 #2](https://github.com/ygandwx/Coinpup/issues/2) 保持开放。
- T02-1：[PR #14](https://github.com/ygandwx/Coinpup/pull/14) 保存此金额增量；是否已合入以 PR 的合并记录为准。通过检查并合入后，接续 T02-2。
- 当前分支有管理员与网页入口；账务接口、记账页面、OCR、提醒和移动端仍未实现。

## 任务状态

| 阶段 | 任务 | 状态 | 说明 |
| --- | --- | --- | --- |
| 1 | T01 工程基础 | 已完成 | PR #13 已合入；登录、网页、迁移、CI 与基础数据库恢复通过 |
| 1 | T02 账务核心 | 进行中 | 首个增量为资产身份与精确金额；账本、账户、分录和余额待实现 |
| 1 | T03 API 与数据契约 | 待开始 | 与 T02 协同实施 |
| 1 | T04 基础网页 | 待开始 | 依赖实际业务接口 |
| 2 | T05 本地票据处理 | 待开始 | 本地识别，人工确认入账 |
| 2 | T06 经营单据 | 待开始 | 含手动和周期 Invoice |
| 2 | T07 代付与报销 | 待开始 | 可选开启 |
| 3 | T08 汇总与估值 | 待开始 | 原币保留，历史与最新估值可切换 |
| 3 | T09 提醒系统 | 待开始 | 含适用税务申报，逐项核实规则 |
| 3 | T10 部署与运维完善 | 待开始 | 自有物理服务器；需恢复演练 |
| 4 | T11 同步服务 | 待开始 | 离线能力留给后续 App |
| 4 | T12 Android / iOS | 待开始 | 两平台构建与设备验证 |

## 本轮已记录的规范

- `AGENTS.md`：统一阅读入口、协作边界与完成要求。
- `CONTRIBUTING.md`：分支、提交、PR、验证与合入流程。
- `handoff.md`：跨环境、跨会话恢复步骤及交接字段。
- `roadmap.md`：四阶段、十二任务、依赖与验收条件。

## 本轮已经实现

- [需求基线](../product/requirements.md)区分已确认范围、设计建议及待配置项；[41 个业务验收案例](../product/acceptance.md)是未来验收目标，未执行。
- [架构概览](../architecture/overview.md)与 [ADR 0001](../architecture/decisions/0001-modular-api-foundation.md)记录 API 方案及金额、文件、同步边界。
- FastAPI 工厂提供 `/api/v1/health/live` 与 `/api/v1/health/ready`；后者检查 PostgreSQL 连接，失败返回 503，隐藏连接异常。配置校验错误文本也隐藏原始输入。
- SQLAlchemy 连接、Alembic 空基线 `20261003_0001` 和认证迁移 `20261003_0002`，管理员、会话及失败登录计数持久化。**尚无账务表**；readiness 不检查迁移版本。
- 依赖锁文件、本地开发密码生成脚本、非 root API 容器与仅绑定本机端口的开发 Compose。
- OpenAPI 快照及一致性检查、文档入口和文件链接检查；CI 配置包含静态检查、单元测试、真实 PostgreSQL 集成及容器启动检查。
- GitHub 已建立 [T01–T12 的 12 个 Issue](https://github.com/ygandwx/Coinpup/issues)，编号 #1–#12 与任务编号一一对应。
- CLI 初始化唯一管理员/重置密码，Argon2id 密码散列、不透明 Cookie 会话、过期/退出/重置撤销、Origin 与 CSRF 检查、数据库共享的失败登录限制。设计见 [ADR 0002](../architecture/decisions/0002-administrator-session.md)。
- React + TypeScript 网页入口使用真实会话接口，中英切换和手机布局；不显示虚构财务余额。
- 数据库备份使用 custom dump、SHA-256 manifest 和私有权限；恢复仅允许指定的空库，拒绝覆盖。尚无附件存储，不宣称附件已备份。见[运维说明](operations.md)。

## T01 验证证据与环境限制

2026-10-03 本地 Windows / Python 3.12.14 执行结果：

| 命令 | 实际结果 |
| --- | --- |
| `python -m ruff check .` | 通过 |
| `python -m ruff format --check .` | 通过 |
| `python -m pytest` | 45 通过、6 跳过；其中 5 项真实 PostgreSQL 和 1 项 POSIX 权限检查留给 CI |
| `python scripts/check_docs.py` | 通过；检查文档入口和仓库内文件链接，不验证网址及锚点 |
| `python scripts/export_openapi.py --check` | 通过；包含健康与认证接口，无账务接口 |
| `python -m alembic upgrade head --sql` | 通过；仅生成离线 SQL，不能替代真实数据库迁移 |
| `npm --prefix apps/web run typecheck` | 通过 |
| `npm --prefix apps/web run build -- --configLoader native` | 通过；本地 Windows 沙箱禁止默认配置打包器枚举根目录，使用 Node 24 原生配置加载；CI 验证标准构建命令 |

- 本地没有 PostgreSQL / Docker，数据库集成测试按显式开关跳过，POSIX 权限检查在 Windows 跳过；对应验证已在以下远端 CI 完成。
- 测试出现一条 Starlette 对 `httpx` TestClient 依赖的弃用警告；当前测试通过，后续依赖更新需评估兼容迁移。
- 尚未部署到用户服务器；T01 工程验收完成不代表完整记账业务或生产可用性验收。

T01 最终代码验证：[GitHub Actions 37117602098](https://github.com/ygandwx/Coinpup/actions/runs/37117602098)，对应提交 `489c33ba895f37e721f4c98938828bf1caf02262`，四个作业全部成功：

- Linux 单元测试 46 通过（包含 POSIX 权限检查），5 个 PostgreSQL 测试在独立作业执行；静态检查、文档与 OpenAPI 一致性通过。
- PostgreSQL 17 迁移升级/降级/再升级、迁移一致性、真实连接及认证集成 5 通过。
- 网页 TypeScript 与标准生产构建通过。
- Docker Compose 构建启动；桌面/手机浏览器 8 个测试通过，登录、刷新、退出及双语入口可操作；[浏览器证据](https://github.com/ygandwx/Coinpup/actions/runs/37117602098/artifacts/11271833282)。
- 备份恢复到新的空数据库，管理员、会话、登录限制与迁移记录一致，原库和恢复库会话均可解析；第二次向非空库恢复被拒绝。

独立代码审查未发现合并阻塞问题；PR #13 在上述代码提交上通过检查后合并。T01 基础恢复只覆盖现有数据库；财务数据与附件恢复仍需随相关业务实现验证。

## T02 当前增量

设计见 [ADR 0003](../architecture/decisions/0003-exact-asset-amounts.md)，实施顺序见[路线图的 T02 增量](roadmap.md)。本分支新增：

- 不可变资产定义：内置五种法币、BTC/ETH/XMR；其他原币可显式配置代码/精度，USDT/USDC 显式配置网络、代币标识和精度。展示币种范围不限制原币扩展。
- `Amount` 使用整数最小单位；严格字符串输入、同资产加减、范围检查和不依赖 Decimal 全局上下文的转换。
- `AmountNumeric(asset)` 提供固定资产的 PostgreSQL 数值绑定边界；拒绝直接绑定原始数值。它不是最终多币种分录模型，后续按行校验资产。
- 无新增业务表、迁移、API、环境变量或依赖。完整 A-07 还需要账户、业务 API 与手续费分录，保持待验收。

本地 Windows / Python 3.12.14：`python -m pytest` 为 **144 通过、19 跳过**；跳过包括 13 个新增 PostgreSQL 数值案例、既有 5 个 PostgreSQL 案例及 1 个 POSIX 权限检查。临时目录使用当前工作区新建目录以避开 Windows 旧 pytest 目录的 ACL 限制。`ruff check`、`ruff format --check`、文档链接与 OpenAPI 一致性检查通过；业务接口未变。独立核心代码审查未发现阻塞问题。

远端验证：[CI 37118641944](https://github.com/ygandwx/Coinpup/actions/runs/37118641944)，对应代码提交 `2e4f2b2c5e6923bca42d63edc2adae84a0868d59`，四项作业全部成功：

- Linux 单元测试 145 通过，18 项数据库测试在独立 PostgreSQL 作业运行。
- PostgreSQL 集成 18 通过，其中新增 13 项覆盖八种内置资产、显式代币精度、38 位边界往返和错误参数到达数据库之前拒绝；只创建事务内临时表，无业务 schema 变更。
- 标准网页构建、8 项桌面/手机浏览器测试和数据库备份恢复回归通过。
- 静态检查、文档与 OpenAPI 一致性通过；独立代码及交接文档审查已完成。

该 CI 验证完整代码增量；之后的交接文字更新未修改运行代码。PR #14 最终提交的 Checks 和合并记录提供最终集成状态。A-07 的账户/API/手续费流程尚未实现，完整案例仍待验收。

## 下一步

1. 核对 PR #14 的最终 Checks 与合并记录；如已合入，从最新 `main` 开始 T02-2，不重复实现精确金额，不提前关闭 #2。
2. T02-2 建立主体/账本归属、独立分类、多币种账户与资产目录的真实迁移及服务；验证跨账本引用拒绝、模板复制后独立修改和已引用资产精度不可改。
3. 与 T03 协同实现分录、余额、转账、幂等和版本契约，用业务案例验证，避免各模块各算余额。
4. 阶段 1 结束前完成完整手动记账闭环和真实财务/附件基础恢复验收；完整部署、定时备份与恢复演练在 T10 验收。

服务器操作系统、域名、邮件、真实公司资料在对应功能开始时配置，不阻塞当前工程基础。

## 后续更新约定

每个 PR 保留当前任务、已实现范围、验收证据、未完成事项与可执行下一步；重要决策链接 ADR。已完成任务不因新会话重置，详细历史通过 Git 和相关 PR 追溯。
