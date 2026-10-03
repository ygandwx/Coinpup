# 当前项目状态

最后更新：2026-10-03（Asia/Shanghai）。本文件记录实际状态；计划与验收条件见 `roadmap.md`。

## 当前工作

- 仓库：[`ygandwx/Coinpup`](https://github.com/ygandwx/Coinpup)，保持私有，未选定开源许可证。
- 当前任务：**T01 工程基础，进行中**。
- 本轮目标：完成 T01 剩余的管理员登录、网页入口、基础备份恢复；全部验收并集成后开始 T02。
- 工作分支：`chore/project-foundation`，尚未合入 `main`。
- PR：[工程基础与跨环境交接规范 #13](https://github.com/ygandwx/Coinpup/pull/13)，本轮扩展完成 T01，正在验收。
- 当前分支有管理员与网页入口；账务接口、记账页面、OCR、提醒和移动端仍未实现。

## 任务状态

| 阶段 | 任务 | 状态 | 说明 |
| --- | --- | --- | --- |
| 1 | T01 工程基础 | 进行中 | 登录、网页入口及数据库恢复增量正在验收；尚未标为完成 |
| 1 | T02 账务核心 | 待开始 | 按已确认业务规则设计与验证 |
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

## 验证证据与环境限制

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

- 本地没有 PostgreSQL / Docker，数据库集成测试按显式开关跳过，POSIX 权限检查在 Windows 跳过；不将跳过计为通过。本轮真实数据库、浏览器与恢复验收待远端 CI。
- 测试出现一条 Starlette 对 `httpx` TestClient 依赖的弃用警告；当前测试通过，后续依赖更新需评估兼容迁移。
- 尚未部署到用户服务器；没有完成任何业务验收或生产可用性验收，也不代表完整 T01 已完成。

历史基础增量验证：[GitHub Actions 运行 37115591010](https://github.com/ygandwx/Coinpup/actions/runs/37115591010)，对应提交 `f52cc4ad04ce0fc5e07e4daa8089db1036bab709`，三个作业成功；它不包含本轮认证、网页和恢复增量的验证：

- 静态检查、测试、文档检查及 OpenAPI 一致性。
- PostgreSQL 17 真实连接、空基线迁移升级/降级/再升级、迁移一致性和集成测试。
- Docker Compose 构建及启动、迁移执行、HTTP readiness 返回成功。

最新实现对应的 CI 结果以 PR 检查记录及本文件随后补充的验收证据为准。

## 下一步

1. 完成真实数据库、浏览器和备份恢复 CI，独立检查后集成 T01；只有验收及集成都完成才关闭 #1。
2. 开始 T02 的主体/账本归属和资产精度模型，先验证金额精确表示与不允许的输入，再建立真实账务迁移及服务。
3. 与 T03 协同实现分录、余额、转账、幂等和版本契约，用业务案例验证，避免各模块各算余额。
4. 阶段 1 结束前完成完整手动记账闭环和真实财务/附件基础恢复验收；完整部署、定时备份与恢复演练在 T10 验收。

服务器操作系统、域名、邮件、真实公司资料在对应功能开始时配置，不阻塞当前工程基础。

## 后续更新约定

每个 PR 保留当前任务、已实现范围、验收证据、未完成事项与可执行下一步；重要决策链接 ADR。已完成任务不因新会话重置，详细历史通过 Git 和相关 PR 追溯。
