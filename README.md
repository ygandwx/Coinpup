# Coinpup

个人与多家公司共用的自托管记账系统，支持独立账本、多币种、票据处理和跨主体汇总。网页先实现，Android / iOS 与离线同步随后实现。现有网页登录入口支持中文和英文切换。

**T01、T02、T04、T05-1 私有文件和 T05-2 票据网页均已完成验收；本轮保存后按用户要求暂停。** 当前代码提供中英账本/地区资料、资产/账户/分类、期初/拆分收支/转账/换汇/手续费、原币流水及更正/取消和逐版本历史。票据目录支持原件上传下载、证据关联与安全重试；本地 OCR 和 App 尚未实现。实际验收、保存与暂停状态见[当前项目状态](docs/engineering/status.md)。

Coinpup is a self-hosted personal and multi-company bookkeeping project. Its bilingual workspace manages ledgers, regional company details, accounts, categories, financial entries, corrections, cancellation and version history. Private uploads, document management and consistent database/file restoration have passed validation. Development pauses at this saved checkpoint pending the user's next instruction; OCR and mobile apps remain unimplemented. No open-source license has been selected for this private repository.

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
- [更正、取消与不可变历史](docs/architecture/decisions/0008-operation-revisions-and-cancellation.md)
- [业务网页、草稿与精确数量](docs/architecture/decisions/0009-business-web-workspace.md)
- [财务网页与待确认提交](docs/architecture/decisions/0010-financial-web-and-retry.md)
- [网页修订与版本历史](docs/architecture/decisions/0011-financial-revision-web.md)
- [私有文件与一致恢复](docs/architecture/decisions/0012-private-files-and-consistent-bundles.md)
- [票据网页与上传恢复](docs/architecture/decisions/0013-document-web-and-upload-recovery.md)

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

端口仅绑定本机，数据库与私有原件分别保存在 `postgres_data` 和 `files_data` 命名卷中；`docker compose down` 保留数据，不要对需要保留的数据使用 `down -v`。Compose 内部使用容器数据库地址，本地 Python 使用 `.env` 中的 localhost 地址。

迁移包含空基线、认证、主体/账本结构、`20261003_0004` 的不可变财务分录与幂等回执、`20261003_0005` 的同资产转账、`20261003_0006` 的换汇与手续费、`20261003_0007` 的版本历史与取消状态，以及 `20261003_0008` 的私有原件/上传回执/流水关联。应用不会自动建表，必须显式执行迁移。忘记密码时，在服务器交互执行 `docker compose exec api python -m coinpup_api.admin reset-password`；此操作撤销全部旧会话。

## 当前结构 API

登录后可使用 `/api/v1/assets`、`/category-templates`、`/entities` 与 `/ledgers/{ledger_id}` 下的 `/accounts`、`/categories`。开发环境 `/docs` 和 `contracts/openapi.json` 提供实际请求/响应字段。

接口沿用登录 Cookie；POST/PATCH 必须同时携带允许的 `Origin` 与会话接口返回的 `X-CSRF-Token`。所有 PATCH 需要当前 `expected_version`，过期版本返回 409。账户可配置多个 `asset_ids`；创建主体时可选择 `personal_default` 或 `business_default` 分类模板。归档保留历史，主体归档会暂停该账本写入。

网页登录后可创建个人和公司账本、切换主体、管理多资产账户和分类、查看原币余额。公司表单包含大陆、香港、美国新墨西哥州/怀俄明州、爱沙尼亚的资料字段；地区编号可稍后补齐，自定义字段保存为字符串。归档可恢复；编辑版本冲突会保留当前草稿，明确重新载入后才替换。结构接口见 ADR 0004，网页边界见 ADR 0009。

## 当前财务 API

网页“流水”中可新增期初、收入、支出、同币种转账和换汇，并分别填写分类拆分、实际数量及额外费用。金额按原币保留。未知保存结果时请使用“重试原提交”或“核对记录”；原命令仅保存在当前页面内存，同一用户重新登录可手动继续。刷新或关闭会丢失待确认内容，此后应先检查流水再新增。资产目录可配置和启停资产；新资产需关联到付款账户才能选择。

流水卡片可以更正或取消，均需填写原因。旧版本冲突时草稿保留，选择“重新载入当前记录”才替换输入；版本历史保留每次入账和冲销。未知修订时，查询既有记录仅提供当前版本，必须原提交重试取得明确回执，不能把查到旧记录误判为修改成功。已取消记录保留历史且不能再次修改。

在 `/api/v1/ledgers/{ledger_id}` 下，`POST /opening-balances`、`POST /income`、`POST /expenses` 分别录入期初、收入、支出；`GET /operations`、`GET /operations/{operation_id}` 和 `GET /balances` 读取记录与原币余额。金额必须是十进制字符串，收支需要交易日、业务归属日和合计相等的分类拆分；单分类也使用一项 `splits`。

每个财务 POST 除 Cookie、Origin、CSRF 外还必须提供 `Idempotency-Key`（1–128 个无空格可见 ASCII 字符）。新增命令在同账本同键同请求时返回原 201 回执；改变请求返回 409。重试应保留原始请求体，金额 `"1.0"` 与 `"1.00"` 属于不同请求。服务端生成的操作 UUID 不要补进先前未指定 ID 的重试体。

期初每账户/资产只允许一次且不算收入，取消后仍保留期初占用标记。余额包括归档账户和停用资产关联，保留状态标志；没有行情换算。主体归档暂停新的财务命令，已经成功的命令仍可重放。旧账户、分类或资产停用后允许精确冲销，但更正的替代内容必须使用有效引用。完整合同见 ADR 0005、ADR 0008 和实际 OpenAPI。

`POST /transfers` 在同一账本的两个不同账户之间转移相同资产，填写 `source_account_id`、`destination_account_id`、`asset_id`、正数 `amount` 与交易日；使用同样的幂等与访问保护。信用卡消费是支出，银行向信用卡还款是转账，本金不会重复计为费用。新增回执和 `latest_posting` 根据 `kind` 区分收支、转账和换汇内容。设计见 ADR 0006。

`POST /exchanges` 保存实际转出和转入的账户、资产与数量，字段为 `source_account_id`、`source_asset_id`、`source_amount` 及对应的 `destination_*`；两种资产必须不同，可以使用同一个多资产账户。换汇本金逐资产配平，不计作收入或费用，也不使用行情覆盖成交数量。设计见 [ADR 0007](docs/architecture/decisions/0007-exchanges-and-explicit-fees.md)。

收入、支出、转账和换汇可附带最多 20 项 `fees`，每项明确 `account_id`、`asset_id`、正数 `amount` 与费用 `category_id`；允许由同账本其他账户或第三种资产扣费，期初不支持手续费。本金与全部费用原子提交：1000 USD 中兑换 100 USD→90 EUR，另付 2 USD 后余额为 898 USD/90 EUR；1 BTC 支付 0.1 BTC 并另付 0.00001 BTC 后为 0.89999000 BTC。旧收支/转账没有费用时响应不新增 `fees` 字段，省略费用与显式 `fees: []` 保持原幂等摘要。

`GET /operations` 和单条查询现在返回 `OperationState`：稳定 ID、当前版本、`active`/`cancelled` 状态及 `latest_posting`。列表默认包含全部状态，可用 `status=active`、`cancelled` 或 `all` 筛选。原新增 POST 的回执保持不变；重试拿到的是当时的回执，客户端应另读当前状态。

在 `/operations/{operation_id}` 下，`POST /corrections` 提交 `expected_version`、非空 `reason` 和带 `kind` 的完整 `replacement`（不含 ID）；`POST /cancellations` 提交版本和原因。成功返回 200 状态回执，旧版本或修改已取消记录返回 409。更正完整冲销本金和全部费用后写入替代内容，取消只冲销且不能重新激活；两者保留稳定 ID、旧凭证和全部历史回执。`GET /history` 按版本读取原因、执行人和精确有符号分录。设计见 [ADR 0008](docs/architecture/decisions/0008-operation-revisions-and-cancellation.md)。

## 当前私有文件 API

在同一 `/api/v1/ledgers/{ledger_id}` 前缀下，先 `POST /uploads` 预留稳定 UUID、`original_filename`、`declared_size` 和可选流水 `operation_id`，再 `PUT /uploads/{id}/content` 上传 `application/octet-stream` 原始字节。写请求需要 Cookie、Origin、CSRF。`GET /uploads/{id}` 可核对完成回执，未知结果需保留原 ID 和完整原件重试；目前不支持分块续传。

`GET /api/v1/files/configuration` 返回上传限额，默认 50 MiB/120 秒。格式为 PDF/JPEG/PNG/WebP；格式签名检查不代表完成 OCR 或恶意文件扫描。同账本同内容提示重复，其他账本独立。`GET /files`、`GET /files/{id}`、`PATCH /files/{id}` 管理标题与归档，`GET /files/{id}/content` 鉴权下载；流水下的 `/operations/{id}/files` 提供证据关联，关联不会增加费用。

本地存储默认 `data/files`，可配置 `COINPUP_FILES_DIRECTORY`；不要把该目录映射为静态网址。数据库备份不含原件，完整原件使用新增 bundle 工具，见[运维说明](docs/engineering/operations.md)。

网页“票据与证件”按账本保存原件；公司资料页有证件入口，流水卡片“票据”可关联已有文件或上传原件。标题冲突保留草稿，明确重新载入后才替换。文件与关联归档分别操作；取消流水仍保留证据，上传或关联不会增加费用。

未知上传结果时保留本页，使用“核对上传”或“重试原上传”；停止传输不代表服务器取消保存。原件和上传标识仅保存在内存，同一用户重新登录可手动继续；刷新或关闭页面后应先查看目录再重新选择。已知内容冲突不能被查询误认为成功。当前只管理原件，本地 OCR 与人工确认入账仍未实现。

## 网页开发

需要 Node.js 24 与 npm。从仓库根目录执行：

```sh
npm --prefix apps/web ci
npm --prefix apps/web run dev -- --host 127.0.0.1
```

打开 `http://127.0.0.1:5173`；Vite 将 `/api` 转发至本机 8000 的 API。先启动 PostgreSQL、迁移并初始化管理员，不提供默认登录密码或网页公开注册。容器构建会编译网页并由 API 同源提供。

`COINPUP_ALLOWED_ORIGINS` 是允许浏览器操作的精确来源列表（JSON），开发默认包含本机 8000 和 5173。生产模式必须显式配置 HTTPS 来源；实际反向代理/TLS 配置应在部署时验证。登录失败默认 15 分钟内最多 5 次，触发 15 分钟临时限制；会话默认有效 12 小时。对应 `COINPUP_LOGIN_*` 和 `COINPUP_SESSION_TTL_SECONDS` 配置见源码与 ADR。

网页会话通过 HttpOnly Cookie 维护，CSRF token 只放在内存，不写入浏览器存储。备份和恢复步骤见[运维说明](docs/engineering/operations.md)，明确区分数据库单独备份与包含原件的 bundle。

## 检查

```sh
python scripts/check.py          # 默认 fast：Python、文档、接口与离线迁移 SQL
python scripts/check.py web      # 前端类型、单元与生产构建
python scripts/check.py --fix    # 先自动修复格式/lint，再执行默认检查
```

`fast` 不运行 PostgreSQL 集成测试；存在 `apps/web/node_modules` 时也检查前端类型和单元测试。`web` 需先安装网页依赖。每一步显示耗时，任一步失败返回非零。

只针对一次性测试数据库显式设置 `COINPUP_DATABASE_URL` 和 `COINPUP_RUN_DB_TESTS=1`，再运行 `python scripts/check.py db`。该命令执行升级、降级到 base、重新升级、模型一致性和集成测试；不要指向已有业务数据或生产数据库。CI 提供独立 PostgreSQL 17 测试库。

更新实现后使用 `python scripts/export_openapi.py` 生成实际接口契约。未来业务接口仅在实现后进入此契约。

真实浏览器测试使用 `BASE_URL`、`E2E_USERNAME`、`E2E_PASSWORD` 指向一次性测试服务，先安装 Playwright Chromium，再运行 `npm --prefix apps/web run test:e2e`。CI 自动创建独立测试管理员，验证登录、刷新、退出、地区资料、账户分类、账本隔离、版本冲突及中英/手机布局；不要向真实管理员账户执行错误密码场景。

## 依赖更新

依赖声明保存在 `pyproject.toml`，运行与开发依赖分别锁定。修改声明后重新生成并验证两份锁文件：

```sh
python -m piptools compile pyproject.toml --output-file requirements.lock --strip-extras --no-emit-index-url --no-emit-trusted-host
python -m piptools compile pyproject.toml --extra dev --output-file requirements-dev.lock --strip-extras --no-emit-index-url --no-emit-trusted-host
```

## 本次工程范围

API 位于 `services/api/`，网页位于 `apps/web/`；`contracts/` 保存已实现 API 的契约，`scripts/` 提供配置与运维入口。`docs/` 保存需求、架构、决策和交接信息，`.github/` 保存 CI 与工作模板。后台识别和移动端目录在对应任务开始时创建。

每个 PR 必须更新状态文档并给出验证证据。任务只有达到路线图验收条件才标记完成；已写计划、已提交代码、CI 通过、已部署是不同状态。
