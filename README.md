# Coinpup

个人与多家公司共用的自托管记账系统，支持独立账本、多币种、票据处理和跨主体汇总。网页先实现，Android / iOS 与离线同步随后实现。现有网页登录入口支持中文和英文切换。

**T01 工程基础已合入，T02/T03 正在推进，尚未完成可用网页闭环。** 已有管理员会话、双语登录入口、独立账本结构、精确期初/收支/拆分/余额、同资产转账与信用卡还款 API；当前 T02-5 增量实现实际换汇与独立手续费，等待最终 CI 验证。更正、业务网页、OCR 和 App 继续按路线图实现。实际验收与集成状态见[当前项目状态](docs/engineering/status.md)。

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
- [独立账本与结构接口](docs/architecture/decisions/0004-owned-ledger-structure.md)
- [原子分录与持久幂等回执](docs/architecture/decisions/0005-atomic-posting-and-receipts.md)
- [同资产转账与信用卡还款](docs/architecture/decisions/0006-same-asset-transfers.md)
- [实际换汇数量与独立手续费](docs/architecture/decisions/0007-exchanges-and-explicit-fees.md)

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

迁移包含空基线、认证、主体/账本结构、`20261003_0004` 的不可变财务分录与幂等回执、`20261003_0005` 的同资产转账约束，以及 `20261003_0006` 的换汇与手续费组成部分。应用不会自动建表，必须显式执行迁移。忘记密码时，在服务器交互执行 `docker compose exec api python -m coinpup_api.admin reset-password`；此操作撤销全部旧会话。

## 当前结构 API

登录后可使用 `/api/v1/assets`、`/category-templates`、`/entities` 与 `/ledgers/{ledger_id}` 下的 `/accounts`、`/categories`。开发环境 `/docs` 和 `contracts/openapi.json` 提供实际请求/响应字段。

接口沿用登录 Cookie；POST/PATCH 必须同时携带允许的 `Origin` 与会话接口返回的 `X-CSRF-Token`。所有 PATCH 需要当前 `expected_version`，过期版本返回 409。账户可配置多个 `asset_ids`；创建主体时可选择 `personal_default` 或 `business_default` 分类模板。归档保留历史，主体归档会暂停该账本写入。

网页登录后的工作区尚未接入这些管理功能。结构接口的设计和限制见 ADR 0004。

## 当前财务 API

在 `/api/v1/ledgers/{ledger_id}` 下，`POST /opening-balances`、`POST /income`、`POST /expenses` 分别录入期初、收入、支出；`GET /operations`、`GET /operations/{operation_id}` 和 `GET /balances` 读取记录与原币余额。金额必须是十进制字符串，收支需要交易日、业务归属日和合计相等的分类拆分；单分类也使用一项 `splits`。

每个财务 POST 除 Cookie、Origin、CSRF 外还必须提供 `Idempotency-Key`（1–128 个无空格可见 ASCII 字符）。同账本同键同请求返回原 201 回执；改变请求返回 409。重试应保留原始请求体，金额 `"1.0"` 与 `"1.00"` 属于不同请求。服务端生成的操作 UUID 不要补进先前未指定 ID 的重试体。

期初每账户/资产只允许一次且不算收入。余额包括归档账户和停用资产关联，保留状态标志；没有行情换算。账本或账户归档后新记账被拒绝，已经成功的命令仍可重放。当前没有财务修改/删除入口，后续通过冲销与替代保留历史。完整合同见 ADR 0005 和实际 OpenAPI。

`POST /transfers` 在同一账本的两个不同账户之间转移相同资产，填写 `source_account_id`、`destination_account_id`、`asset_id`、正数 `amount` 与交易日；使用同样的幂等与访问保护。信用卡消费是支出，银行向信用卡还款是转账，本金不会重复计为费用。流水根据 `kind` 返回原收支回执或明确包含双方账户的转账回执。设计见 ADR 0006。

`POST /exchanges` 保存实际转出和转入的账户、资产与数量，字段为 `source_account_id`、`source_asset_id`、`source_amount` 及对应的 `destination_*`；两种资产必须不同，可以使用同一个多资产账户。换汇本金逐资产配平，不计作收入或费用，也不使用行情覆盖成交数量。设计见 [ADR 0007](docs/architecture/decisions/0007-exchanges-and-explicit-fees.md)。

收入、支出、转账和换汇可附带最多 20 项 `fees`，每项明确 `account_id`、`asset_id`、正数 `amount` 与费用 `category_id`；允许由同账本其他账户或第三种资产扣费，期初不支持手续费。本金与全部费用原子提交：1000 USD 中兑换 100 USD→90 EUR，另付 2 USD 后余额为 898 USD/90 EUR；1 BTC 支付 0.1 BTC 并另付 0.00001 BTC 后为 0.89999000 BTC。旧收支/转账没有费用时响应不新增 `fees` 字段，省略费用与显式 `fees: []` 保持原幂等摘要。

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
