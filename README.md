# Coinpup

个人与多家公司共用的自托管记账系统，采用独立账本、多币种和私有票据。网页联网使用，Android / iOS 通过共用 API 扩展。实际能力与限制见[当前状态](docs/engineering/status.md)。

## 快速开始

需要 Python 3.12、PostgreSQL 17；网页开发需要 Node.js 24 与 npm。容器方式需要 Docker Compose。密码、私有文件和数据库备份不加入 Git。

### 本地 Python

从仓库根目录建立虚拟环境。Windows 激活命令为 `.venv\Scripts\Activate.ps1`，Linux/macOS 为 `source .venv/bin/activate`。

```sh
python -m venv .venv
# 激活虚拟环境后执行
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
python scripts/init_env.py
python -m alembic upgrade head
python -m coinpup_api.admin create --username admin
python -m uvicorn coinpup_api.main:create_app --factory --app-dir services/api/src --reload --host 127.0.0.1
```

先启动 PostgreSQL，并在未提交的 `.env` 中配置连接；`init_env.py` 不覆盖已有配置。迁移必须显式执行，应用不自动建表。管理员初始化交互输入密码，没有默认账户或公开注册。数据库不可用时 readiness 返回 503；liveness 仅说明进程存活。

### Docker Compose

```sh
python scripts/init_env.py
docker compose up -d --build
docker compose exec api python -m alembic upgrade head
docker compose exec api python -m coinpup_api.admin create --username admin
```

打开 `http://127.0.0.1:8000/`。端口只绑定本机；数据库与私有原件分别放在 `postgres_data` 和 `files_data` 卷。不要对需要保留的数据运行 `docker compose down -v`。密码重置、数据库/原件 bundle、空目标恢复和生产配置见[运维说明](docs/engineering/operations.md)。

### 网页开发

```sh
npm --prefix apps/web ci
npm --prefix apps/web run dev -- --host 127.0.0.1
```

打开 `http://127.0.0.1:5173/`；Vite 将 `/api` 转发到本机 8000 的 API。先启动数据库、迁移并初始化管理员。生产容器会构建网页并由 API 同源提供；HTTPS 来源、反向代理与 TLS 配置见运维说明。

## 检查

```sh
python scripts/check.py          # 默认 fast：Python、文档、接口、离线迁移 SQL
python scripts/check.py web      # 前端类型、单元和生产构建
python scripts/check.py --fix    # 先自动修复 Python lint/格式，再运行默认检查
```

`fast` 不运行数据库集成测试；安装网页依赖后也运行类型和单元检查。每一步显示名称、耗时和结果，失败返回非零。

仅针对一次性测试库显式设置 `COINPUP_RUN_DB_TESTS=1` 和 `COINPUP_DATABASE_URL`，再运行 `python scripts/check.py db`。它会降级到 base 并运行破坏性集成夹具，不能指向真实业务库。CI 提供独立 PostgreSQL 17。

真实浏览器测试先安装 Playwright Chromium，用 `BASE_URL`、`E2E_USERNAME`、`E2E_PASSWORD` 指向一次性测试服务，再运行 `npm --prefix apps/web run test:e2e`。CI 会创建独立测试管理员并执行容器恢复演练；不要向真实管理员执行错误密码测试。验证命令、结果和 CI 证据记录在对应 PR。

## 依赖更新

Python 声明保存在 `pyproject.toml`，修改后重新生成并验证运行与开发锁文件：

```sh
python -m piptools compile pyproject.toml --output-file requirements.lock --strip-extras --no-emit-index-url --no-emit-trusted-host
python -m piptools compile pyproject.toml --extra dev --output-file requirements-dev.lock --strip-extras --no-emit-index-url --no-emit-trusted-host
```

网页依赖修改 `apps/web/package.json` 后同步提交 `package-lock.json`。依赖更新执行统一检查与相关真实流程。

## 文档地图

| 内容 | 入口 |
| --- | --- |
| 开发规则与接续 | [AGENTS.md](AGENTS.md) |
| 最新授权、进度、限制和下一步 | [status.md](docs/engineering/status.md) |
| 任务依赖与验收条件 | [roadmap.md](docs/engineering/roadmap.md) |
| 优化工作包与状态 | [optimization-plan.md](docs/engineering/optimization-plan.md) |
| 产品需求与业务验收 | [requirements.md](docs/product/requirements.md)、[acceptance.md](docs/product/acceptance.md) |
| 稳定架构与业务不变量 | [overview.md](docs/architecture/overview.md) |
| 跨接口通用约定 | [api-conventions.md](docs/architecture/api-conventions.md) |
| 设计决定 | [ADR 索引](docs/architecture/decisions/README.md) |
| 单个接口字段 | [OpenAPI 契约](contracts/openapi.json)，由 `python scripts/export_openapi.py` 生成；开发环境 `/docs` 可浏览 |
| 配置、备份、恢复和部署 | [operations.md](docs/engineering/operations.md) |

API 位于 `services/api/`，网页位于 `apps/web/`，运维入口位于 `scripts/`。API 约定和契约供网页及后续客户端共同使用。
