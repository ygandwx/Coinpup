# Coinpup

个人与多家公司共用的自托管记账系统，支持独立账本、多币种、票据处理和跨主体汇总。网页先实现，Android / iOS 与离线同步随后实现。界面计划支持中文和英文。

**当前是工程基础，尚不能用于真实记账。** 已有健康检查 API、PostgreSQL 连接配置、Alembic 迁移入口、自动检查和可接续的项目文档。登录、业务接口、网页、OCR 和 App 尚未实现。精确状态见[当前项目状态](docs/engineering/status.md)。

Coinpup is a self-hosted personal and multi-company bookkeeping project. This first increment provides engineering foundations only, not a usable finance application. No open-source license has been selected for this private repository.

## 从这里开始

- [开发入口与协作约定](AGENTS.md)
- [当前状态与可执行下一步](docs/engineering/status.md)
- [四阶段和十二个工作包](docs/engineering/roadmap.md)
- [已确认需求](docs/product/requirements.md)与[业务验收案例](docs/product/acceptance.md)
- [架构设计](docs/architecture/overview.md)与[初始架构决定](docs/architecture/decisions/0001-modular-api-foundation.md)
- [贡献流程](CONTRIBUTING.md)与[跨环境交接](docs/engineering/handoff.md)

新位置开始工作时先阅读以上入口，核对 Git 分支、PR、CI 和未提交改动，再继续状态文档中的下一步。

## 本地 Python 开发

需要 Python 3.12。从仓库根目录执行；Windows 激活命令为 `.venv\Scripts\Activate.ps1`，Linux/macOS 为 `source .venv/bin/activate`。

```sh
python -m venv .venv
# 使用上面的对应平台命令激活环境
python -m pip install -r requirements-dev.lock
python scripts/init_env.py
python -m uvicorn coinpup_api.main:create_app --factory --app-dir services/api/src --reload --host 127.0.0.1
```

`init_env.py` 为本地开发生成密码且不会覆盖已有 `.env`。虚拟环境、数据库、密钥和真实票据均不进入 Git。

- `GET /api/v1/health/live`：进程存活，不依赖数据库。
- `GET /api/v1/health/ready`：检查数据库连通性；不可用时返回 503，不披露连接详情。此接口不验证迁移版本，也不代表业务功能已完成。
- `http://127.0.0.1:8000/docs`：开发环境接口文档。生产模式关闭 API 浏览器；本项目仍未完成生产鉴权与部署验收。

若没有启动 PostgreSQL，liveness 可成功而 readiness 会返回 503，这是预期行为。只运行单元检查不需要数据库。

## 使用 Docker Compose 启动开发数据库和 API

安装 Docker Compose 后，在根目录执行（已有 `.env` 时跳过生成）：

```sh
python scripts/init_env.py
docker compose up -d --build
docker compose exec api python -m alembic upgrade head
docker compose exec api python -m alembic current
```

访问 `http://127.0.0.1:8000/api/v1/health/ready`。端口仅绑定本机，数据库保存在命名卷中；`docker compose down` 保留数据，不要对需要保留的数据使用 `down -v`。Compose 内部使用容器数据库地址，本地 Python 使用 `.env` 中的 localhost 地址。

当前基线迁移只建立 Alembic 版本链，尚无业务数据表。后续表结构必须通过新迁移引入，不在应用启动时自动创建或修改表。

## 检查

```sh
python -m ruff check .
python -m ruff format --check .
python -m pytest
python scripts/check_docs.py
python scripts/export_openapi.py --check
python -m alembic upgrade head --sql
```

默认跳过 PostgreSQL 集成测试。仅针对一次性测试数据库设置 `COINPUP_DATABASE_URL` 和 `COINPUP_RUN_DB_TESTS=1`，先执行 `python -m alembic upgrade head`，再运行 `python -m pytest tests/integration -m integration`。CI 会提供独立 PostgreSQL 17 数据库；不要将这些检查指向生产数据库。

更新实现后使用 `python scripts/export_openapi.py` 生成实际接口契约。未来业务接口仅在实现后进入此契约。

## 依赖更新

依赖声明保存在 `pyproject.toml`，运行与开发依赖分别锁定。修改声明后重新生成并验证两份锁文件：

```sh
python -m piptools compile pyproject.toml --output-file requirements.lock --strip-extras --no-emit-index-url --no-emit-trusted-host
python -m piptools compile pyproject.toml --extra dev --output-file requirements-dev.lock --strip-extras --no-emit-index-url --no-emit-trusted-host
```

## 本次工程范围

当前可执行代码位于 `services/api/`；`contracts/` 保存已实现 API 的契约。`docs/` 保存需求、架构、决策和交接信息，`.github/` 保存 CI 与工作模板。网页、后台识别工作进程和移动端目录在对应任务开始时创建。

每个 PR 必须更新状态文档并给出验证证据。任务只有达到路线图验收条件才标记完成；已写计划、已提交代码、CI 通过、已部署是不同状态。
