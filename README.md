# Coinpup

个人与多家公司共用的自托管记账系统，支持独立账本、多币种、票据处理和跨主体汇总。网页先实现，Android / iOS 与离线同步随后实现。界面计划支持中文和英文。

**T01 工程基础已合入，T02 账务核心正在开发，尚不能用于真实记账。** 已有管理员初始化、登录/退出、双语网页入口、数据库会话、健康 API、迁移、基础数据库备份恢复及自动检查。T02 从精确金额与资产身份开始；账务接口、记账页面、OCR 和 App 尚未实现。实际验收与集成状态见[当前项目状态](docs/engineering/status.md)。

Coinpup is a self-hosted personal and multi-company bookkeeping project. The current foundation includes administrator sessions and a bilingual web entry, but is not yet a usable finance application. No open-source license has been selected for this private repository.

## 从这里开始

- [开发入口与协作约定](AGENTS.md)
- [当前状态与可执行下一步](docs/engineering/status.md)
- [四阶段和十二个工作包](docs/engineering/roadmap.md)
- [已确认需求](docs/product/requirements.md)与[业务验收案例](docs/product/acceptance.md)
- [架构设计](docs/architecture/overview.md)与[初始架构决定](docs/architecture/decisions/0001-modular-api-foundation.md)
- [贡献流程](CONTRIBUTING.md)与[跨环境交接](docs/engineering/handoff.md)
- [管理员与会话设计](docs/architecture/decisions/0002-administrator-session.md)与[备份恢复操作](docs/engineering/operations.md)
- [精确金额与资产身份](docs/architecture/decisions/0003-exact-asset-amounts.md)

新位置开始工作时先阅读以上入口，核对 Git 分支、PR、CI 和未提交改动，再继续状态文档中的下一步。

## 本地 Python 开发

需要 Python 3.12。从仓库根目录执行；Windows 激活命令为 `.venv\Scripts\Activate.ps1`，Linux/macOS 为 `source .venv/bin/activate`。

```sh
python -m venv .venv
# 使用上面的对应平台命令激活环境
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
python scripts/init_env.py
python -m uvicorn coinpup_api.main:create_app --factory --app-dir services/api/src --reload --host 127.0.0.1
```

`init_env.py` 为本地开发生成密码且不会覆盖已有 `.env`。虚拟环境、数据库、密钥和真实票据均不进入 Git。

- `GET /api/v1/health/live`：进程存活，不依赖数据库。
- `GET /api/v1/health/ready`：检查数据库连通性；不可用时返回 503，不披露连接详情。此接口不验证迁移版本，也不代表业务功能已完成。
- `http://127.0.0.1:8000/docs`：开发环境接口文档。生产模式关闭 API 浏览器；实际服务器的 HTTPS、备份位置和部署验收尚待配置。

若没有启动 PostgreSQL，liveness 可成功而 readiness 会返回 503，这是预期行为。只运行单元检查不需要数据库。

## 使用 Docker Compose 启动开发数据库和 API

安装 Docker Compose 后，在根目录执行（已有 `.env` 时跳过生成）：

```sh
python scripts/init_env.py
docker compose up -d --build
docker compose exec api python -m alembic upgrade head
docker compose exec api python -m alembic current
docker compose exec api python -m coinpup_api.admin create --username admin
```

最后一条命令交互输入密码（12–128 字符），只允许初始化一个管理员。打开 `http://127.0.0.1:8000/` 登录；可在中英文之间切换，刷新后会话由服务端验证，退出会撤销会话。

端口仅绑定本机，数据库保存在命名卷中；`docker compose down` 保留数据，不要对需要保留的数据使用 `down -v`。Compose 内部使用容器数据库地址，本地 Python 使用 `.env` 中的 localhost 地址。

迁移包含空基线及管理员、会话、登录限制表，尚无账务数据表。应用不会自动建表，必须显式执行迁移。忘记密码时，在服务器交互执行 `docker compose exec api python -m coinpup_api.admin reset-password`；此操作撤销全部旧会话。

## 网页开发

需要 Node.js 24 与 npm。从仓库根目录执行：

```sh
npm --prefix apps/web ci
npm --prefix apps/web run dev -- --host 127.0.0.1
```

打开 `http://127.0.0.1:5173`；Vite 将 `/api` 转发至本机 8000 的 API。先启动 PostgreSQL、迁移并初始化管理员，不提供默认登录密码或网页公开注册。容器构建会编译网页并由 API 同源提供。

`COINPUP_ALLOWED_ORIGINS` 是允许浏览器操作的精确来源列表（JSON），开发默认包含本机 8000 和 5173。生产模式必须显式配置 HTTPS 来源；实际反向代理/TLS 配置应在部署时验证。登录失败默认 15 分钟内最多 5 次，触发 15 分钟临时限制；会话默认有效 12 小时。对应 `COINPUP_LOGIN_*` 和 `COINPUP_SESSION_TTL_SECONDS` 配置见源码与 ADR。

网页会话通过 HttpOnly Cookie 维护，CSRF token 只放在内存，不写入浏览器存储。备份和恢复步骤见[运维说明](docs/engineering/operations.md)，当前只覆盖数据库；文件附件尚未实现。

## 检查

```sh
python -m ruff check .
python -m ruff format --check .
python -m pytest
python scripts/check_docs.py
python scripts/export_openapi.py --check
python -m alembic upgrade head --sql
npm --prefix apps/web run typecheck
npm --prefix apps/web run build
```

默认跳过 PostgreSQL 集成测试。仅针对一次性测试数据库设置 `COINPUP_DATABASE_URL` 和 `COINPUP_RUN_DB_TESTS=1`，先执行 `python -m alembic upgrade head`，再运行 `python -m pytest tests/integration -m integration`。CI 会提供独立 PostgreSQL 17 数据库；不要将这些检查指向生产数据库。

更新实现后使用 `python scripts/export_openapi.py` 生成实际接口契约。未来业务接口仅在实现后进入此契约。

真实浏览器测试使用 `BASE_URL`、`E2E_USERNAME`、`E2E_PASSWORD` 指向一次性测试服务，先安装 Playwright Chromium，再运行 `npm --prefix apps/web run test:e2e`。CI 自动创建独立测试管理员，验证登录、刷新、退出及中英/手机布局；不要向真实管理员账户执行错误密码场景。

## 依赖更新

依赖声明保存在 `pyproject.toml`，运行与开发依赖分别锁定。修改声明后重新生成并验证两份锁文件：

```sh
python -m piptools compile pyproject.toml --output-file requirements.lock --strip-extras --no-emit-index-url --no-emit-trusted-host
python -m piptools compile pyproject.toml --extra dev --output-file requirements-dev.lock --strip-extras --no-emit-index-url --no-emit-trusted-host
```

## 本次工程范围

API 位于 `services/api/`，网页位于 `apps/web/`；`contracts/` 保存已实现 API 的契约，`scripts/` 提供配置与运维入口。`docs/` 保存需求、架构、决策和交接信息，`.github/` 保存 CI 与工作模板。后台识别和移动端目录在对应任务开始时创建。

每个 PR 必须更新状态文档并给出验证证据。任务只有达到路线图验收条件才标记完成；已写计划、已提交代码、CI 通过、已部署是不同状态。
