# Coinpup 优化计划（Codex 执行版）

> **给用户的使用方法**
>
> 1. 把本文件放到仓库的 `docs/engineering/optimization-plan.md`，并在 `AGENTS.md` 末尾加一行：
>    `进行中的优化计划：docs/engineering/optimization-plan.md（执行规则见其第 0 节）`
> 2. **第一次**给 Codex 的指令（它会替代 `status.md` 中“暂停、不得启动其他任务”的指示）：
>    `新的指示：暂缓 T05-3 OCR，先执行 docs/engineering/optimization-plan.md。阅读其第 0 节和状态表，执行下一个可以开始的工作包；只做这一个包，按验收标准自检并运行检查，在同一个 PR 中更新状态表；完成后停下等我。遇到 🛑 包只提交 ADR 草稿，然后等我确认。`
> 3. 之后每次只需说：`继续执行优化计划的下一个工作包。` 想跳过顺序时，直接写编号，例如“执行 OPT-23”。
> 4. 如果使用 Codex 云端任务，环境 setup 脚本可以设为：
>    `python -m pip install -r requirements-dev.lock && python -m pip install --no-deps -e . && npm --prefix apps/web ci`
>
> 依据：2026-10-04 对 `main` 的代码与文档评审，基准是合并 PR #24 后的保存点 `4473ad2`。本文件是一次性计划，全部完成后删除（见第 6 节）。

---

## 0. 给 Codex 的执行规则（每次必读）

1. **一次只做一个工作包（OPT-NN）**。从第 2 节状态表中选择第一个状态为“待开始”、且所有依赖都已“已完成”的包。用户指定了编号时，按用户指定执行。用户在当前会话中的最新指令，优先于 `status.md` 中记录的旧指示。
2. **一个包对应一个分支、一个 PR**。分支名用 `chore/opt-NN-<主题>`，纯重构用 `refactor/opt-NN-<主题>`。包内注明“可拆分”的，可以拆成多个 PR，但每个 PR 都要能单独通过检查。
3. **🛑 包是需要用户拍板的设计**。只提交 ADR 草稿：PR 设为 Draft，标题加前缀 `[需确认]`，写清可选方案、推荐方案、对验收案例的影响。然后停止，不写实现代码。用户在 PR 中确认后，再开新的 PR 实现。
4. **不得削弱第 1.2 节的不变量**。除非包内明确要求，不改变任何 API 行为、响应 JSON 结构、数据库语义或幂等结果。
5. **每个 PR 完成前都要运行检查**。OPT-02 完成前，用 `README.md`“检查”一节的命令；完成后，用 `python scripts/check.py`。在 PR 描述中写明实际执行的命令和结果；无法运行的检查要写明原因。环境里没有 PostgreSQL 时，数据库检查交给 CI 的 `database` job，并在 PR 中注明。
6. **在同一个 PR 中更新本文件第 2 节的状态表**：把对应行改为“已完成”或“待确认”，并填上 PR 编号。用户授权拆分的包，中间 PR 列明已完成子项并保持整包“进行中”；最后一个 PR 再标记整包“已完成”。
7. **同一时间只允许一个含数据库迁移的 PR 处于打开状态**。合并前先 rebase 到最新的 `main`，并确认 `python -m alembic heads` 只输出一个 head。
8. **发现计划与代码不符，或某一步会破坏现有测试时，停在这一步**。在 PR 中说明情况，并提出修订后的步骤。不要为了“完成计划”而绕过、跳过或删除测试。
9. 测试和示例只使用明确虚构的数据，不提交任何密钥。
10. ADR 编号使用下一个可用编号，不要预先占用。当前最后一个是 0013。

---

## 1. 背景与原则

### 1.1 项目现实

- 只有一位使用者，系统自托管。个人和多家公司（中国大陆、香港、美国 NM/WY、爱沙尼亚）各自独立记账，支持多币种和加密资产。
- 主要开发者是 Codex。仓库文档就是 Codex 的工作记忆，所以必须短、准、不重复：**同一事实只出现在一个位置**。
- 优先级依次为：账务正确性 > 可恢复性 > 开发效率 > 性能 > 通用性。

### 1.2 必须保留的不变量（任何包都不得削弱）

- **金额**：用十进制字符串传输，按整数最小单位计算，数据库类型为 `NUMERIC(38,18)`。后端禁止使用 float；前端禁止 `Number`/`parseFloat`，计算一律用 BigInt。
- **分录**：每种资产各自配平；提交后封存，禁止 UPDATE/DELETE。更正 = 完整冲销 + 替代分录；取消 = 只冲销。版本链必须连续。
- **幂等**：财务写命令必须带 `Idempotency-Key`。成功回执永久保存，重放时返回原回执。
- **隔离**：所有数据都归属于某个账本或主体；跨账本引用由数据库复合外键拒绝。
- **访问**：Cookie 会话 + Origin + `X-CSRF-Token`。附件只能鉴权后下载，不作为静态资源暴露。
- **迁移**：显式执行，使用冻结的 SQL；存在历史数据时拒绝降级。
- **备份恢复**：数据库和原件 bundle 都要在空库上验证能恢复。
- **未知提交结果**：网页端保留原请求重试，不重新生成幂等键或上传 ID（`PendingCommandController`、`pending-upload.ts`）。

### 1.3 需要削减的成本（已核实，截至 `4473ad2`）

- **进度描述重复**：README、overview、status 都在描述进度。每完成一个增量，就得同时改这三处（PR #24 就是这样）；漏改任何一处，文档就会过期（PR #24 之前 overview 一度还停留在 T04-3）。
- **`status.md` 越写越长**：“已集成基线”一节每个 PR 都追加一段 CI 运行编号和测试数量，目前已有 70 行、11 KB，还在增长。
- **每次开工必读约 81 KB 文档**（README 16 KB、overview 21 KB、requirements 23 KB、status 11 KB、roadmap 8 KB 等），占用大量上下文。
- **每个 PR 都要改 `status.md`**：并行任务之间必然冲突。
- **CI 重复运行**：`.github/workflows/ci.yml` 对所有分支的 `push` 和所有 `pull_request` 都会触发。两者的并发组不同、互不取消，于是同一提交会跑两遍，白白消耗私有仓库的 Actions 分钟数。
- **前端没有格式化和 lint 工具**：`apps/web` 中没有 Prettier 或 ESLint。`Workspace.tsx` 单行最长 2,072 字符；`PostingForm.tsx` 有 66 行、`DocumentsPanel.tsx` 有 41 行超过 160 字符。差异难以审阅，容易冲突，也很难精确修改。后端则有 ruff。

### 1.4 明确不做（个人项目不需要）

多租户、角色权限、微服务拆分、Kubernetes、Redis/Celery/Kafka 等中间件、外部对象存储、GraphQL、事件溯源重写、修改 Python 包名 `coinpup_api`、改用 async SQLAlchemy、改写已合并的历史 ADR。

---

## 2. 状态表

| 编号 | 标题 | 阶段 | 依赖 | 状态 | PR |
| --- | --- | --- | --- | --- | --- |
| OPT-01 | CI 触发去重 | A 流程与结构 | 无 | 已完成 | #26 |
| OPT-02 | 统一检查入口 `scripts/check.py` | A | 无 | 已完成 | #27 |
| OPT-03 | 文档单一事实来源与瘦身 | A | OPT-02 | 已完成 | #28 |
| OPT-04 | AGENTS.md 分层与开发循环 | A | OPT-03 | 已完成 | #29 |
| OPT-05 | PR 模板、并行规则、GitHub 设置 | A | OPT-04 | 已完成 | #30 |
| OPT-06 | 前端格式化与 lint | A | OPT-02 | 已完成 | #31 |
| OPT-07 | 前端类型由 OpenAPI 生成 | A | OPT-06 | 已完成 | #32 |
| OPT-08 | 模块整理与 documents→files 改名（零行为变化） | A | OPT-02、OPT-06 | 进行中（8a/8b/8d 已完成；8c 待开始） | #33 |
| OPT-09 | Python 类型检查（可选） | A | OPT-08 | 待开始 | |
| OPT-10 | 读余额不加行锁 | B 低风险改进 | OPT-08 | 待开始 | |
| OPT-11 | 余额与日期查询索引 | B | OPT-08 | 待开始 | |
| OPT-12 | 流水列表批量读取与按日期浏览 | B | OPT-07、OPT-11 | 待开始 | |
| OPT-20 | 幂等摘要 v2 | C 结构性改进 | OPT-08 | 待开始 | |
| OPT-21 | 数据库形状校验按业务类型分发 | C | OPT-08 | 待开始 | |
| OPT-22 | 变更日志与同步游标 | C | OPT-08 | 待开始 | |
| OPT-23 🛑 | 资产负债类账户与维度 | C | 无（ADR 可随时写） | 待开始 | |
| OPT-24 🛑 | 跨账本关联业务 | C | OPT-23 | 待开始 | |
| OPT-25 🛑 | 结账与期间锁定 | C | 无（ADR 可随时写） | 待开始 | |
| OPT-30 🛑 | 登录防护与暴露面 | D 部署前 | 无 | 待开始 | |

---

## 3. 目标目录结构（A 阶段完成后）

```text
Coinpup/
├── AGENTS.md                         # Codex 总规则（≤ 8 KB，含子目录路由）
├── README.md                         # ≤ 8 KB：定位、快速开始、检查、文档地图
├── .git-blame-ignore-revs            # 记录纯格式化提交（OPT-06）
├── .github/
│   ├── workflows/ci.yml              # push 只触发 main；调用 scripts/check.py
│   ├── pull_request_template.md      # 精简版
│   ├── dependabot.yml                # 可选：npm 与 github-actions，每月分组
│   └── ISSUE_TEMPLATE/               # 不变
├── apps/
│   ├── web/
│   │   ├── AGENTS.md                 # 前端规则
│   │   ├── .prettierrc.json  .prettierignore  eslint.config.js
│   │   └── src/
│   │       ├── generated/openapi.ts  # 由 contracts/openapi.json 生成，禁止手改
│   │       ├── FilesPanel.tsx  files-api.ts  files.css   # 原 Documents*/document-api/documents.css
│   │       └── …                     # 其余文件不变
│   └── mobile/                       # T12 时再创建
├── services/api/
│   ├── AGENTS.md                     # 后端与账务规则、测试矩阵、迁移规则、模块放置约定
│   ├── migrations/
│   └── src/coinpup_api/
│       ├── main.py  auth.py  admin.py  config.py  database.py  security.py  access.py  models.py
│       ├── ledger/
│       │   ├── models.py  money.py  assets.py  templates.py  errors.py
│       │   ├── service.py  router.py  schemas.py             # 结构管理（不变）
│       │   ├── posting.py                                   # 只保留 PostingService 门面
│       │   ├── commands/  classified.py  transfer.py  exchange.py  fees.py
│       │   ├── idempotency.py  readers.py  balances.py  revisions.py
│       │   └── posting_router.py  posting_schemas.py  posting_storage.py  persistence.py
│       └── files/                    # 原 documents/（私有文件），表名与 URL 不变
├── contracts/openapi.json
├── docs/
│   ├── architecture/
│   │   ├── overview.md               # 只写稳定架构，不写进度
│   │   ├── api-conventions.md        # 新增：跨接口通用约定
│   │   └── decisions/README.md       # 新增：ADR 索引；NNNN-*.md 正文不改
│   ├── engineering/
│   │   ├── status.md                 # ≤ 6 KB
│   │   ├── roadmap.md
│   │   ├── operations.md
│   │   └── optimization-plan.md      # 本文件，完成后删除
│   └── product/requirements.md  acceptance.md               # 业务内容不改
├── scripts/check.py                  # 新增：统一检查入口；其余运维脚本不变
└── tests/
    ├── unit/{app,auth,ledger,files,ops}/
    └── integration/{auth,ledger,files}/
```

要删除的文件：`CONTRIBUTING.md` 和 `docs/engineering/handoff.md`，其中仍有效的内容并入 `AGENTS.md`。

---

## 4. 工作包

### OPT-01 CI 触发去重

- **现状**：`on: push:` 没有限制分支，同时还配置了 `pull_request:`。两者的并发组分别按 ref 和 PR 号计算，互不取消，所以同一提交会跑两遍，每遍 4 个 job，其中容器加浏览器的 job 最长可达 25 分钟。
- **改动**：`push` 只保留 `branches: [main]`；保留 `pull_request` 和 `workflow_dispatch`；各 job 的内容不变。
- **可选**（单独提交，并写明取舍）：只改文档的 PR 跳过 `database` 和 `container` job。注意：如果以后在 GitHub 上设置了“必需检查”，整个 workflow 被跳过会让 PR 一直处于等待状态。正确做法是保留 workflow 的触发，在 job 内按变更路径判断，并增加一个始终运行的汇总 job，把它作为唯一的必需检查。拿不准就不要做这个可选项。
- **验收**：向 PR 分支推送只产生一次 CI 运行；合并到 `main` 产生一次运行。

### OPT-02 统一检查入口 `scripts/check.py`

- **现状**：README“检查”一节列了 9 条命令，CI 则分散执行。其中 `alembic upgrade head --sql` 只写在 README 里，CI 并没有执行。用户的本地开发机是 Windows，CI 是 Linux。
- **改动**：新增只依赖标准库的 `scripts/check.py`，提供以下子命令：
  - `python scripts/check.py`（默认等同 `fast`）：依次执行
    1. `ruff check`、`ruff format --check`；
    2. `pytest`（不含数据库测试）；
    3. `scripts/check_docs.py`、`scripts/export_openapi.py --check`；
    4. `alembic upgrade head --sql`（丢弃输出，只看是否成功）；
    5. 如果 `apps/web/node_modules` 存在，再执行 `npm run typecheck` 和 `npm run test:unit`；不存在则提示跳过。
  - `web`：执行 typecheck、test:unit、build。OPT-06 完成后加入格式与 lint 检查，OPT-07 完成后加入生成类型的一致性检查。
  - `db`：要求已设置 `COINPUP_RUN_DB_TESTS=1` 和 `COINPUP_DATABASE_URL`，否则报错退出。依次执行 upgrade head → downgrade base → upgrade head → `alembic check` → `pytest tests/integration -m integration`。运行前醒目提示：只能指向一次性测试库。
  - `--fix`：先执行 `ruff check --fix` 和 `ruff format`；OPT-06 完成后再加上 `npm run format`。
  - **跨平台要求**：用 `sys.executable -m ...` 调用 Python 工具；用 `shutil.which("npm")` 查找 npm（Windows 上是 `npm.cmd`）；不使用 shell 管道。
  - 每一步打印名称和耗时，最后输出通过/失败汇总；任何一步失败都返回非零退出码。
- **CI 改动**：`quality` job 改为运行 `python scripts/check.py fast`（该 job 没有 node_modules，会自动跳过网页检查）；`database` job 改为运行 `python scripts/check.py db`；`web` job 可以继续直接调用 npm，只要执行的检查与 `check.py web` 一致。
- **同步**：README“检查”一节缩短为 `check.py` 的用法。
- **验收**：Windows 和 Linux 上都能运行；CI 执行的检查集合不少于改动前，而且新增了离线迁移 SQL 检查。

### OPT-03 文档单一事实来源与瘦身

**“一事一处”规则：**

| 事实类型 | 唯一位置 | 其他文档 |
| --- | --- | --- |
| 产品需求（C/P 条目） | `docs/product/requirements.md` | 只放链接 |
| 业务验收案例 | `docs/product/acceptance.md` | 只放链接 |
| 稳定架构、模块边界、业务不变量 | `docs/architecture/overview.md` | 不写进度 |
| 跨接口通用约定 | 新建 `docs/architecture/api-conventions.md` | README 不复述 |
| 单个接口的字段 | `contracts/openapi.json`（由代码生成） | Markdown 不复述字段 |
| 设计决定 | `docs/architecture/decisions/NNNN-*.md`（合并后不改正文）+ 索引 | — |
| 任务、依赖、验收条件 | `docs/engineering/roadmap.md` | — |
| 用户最新指示、当前进度、下一步、已知限制 | `docs/engineering/status.md` | 其他文档不写“进行中”“已完成”“已暂停”之类的进度 |
| 运维操作 | `docs/engineering/operations.md` | — |
| 开发规则（含 Codex 规则） | `AGENTS.md`（见 OPT-04） | — |
| 验证证据（命令、结果、CI 链接、测试数量） | PR 描述 | 不写入仓库文档 |

**步骤：**

1. **新建 `api-conventions.md`（≤ 150 行）**。从 README 的“当前结构 API”“当前财务 API”“当前私有文件 API”三节，以及 ADR 0002、0004、0005、0008、0012、0013 中提取跨接口的通用规则：
   - Cookie + Origin + `X-CSRF-Token`；
   - `expected_version` 与 409；
   - `Idempotency-Key` 的格式、作用域（账本）和摘要规则：金额字符串的拼写是敏感的；重试必须保留原请求体；不要把服务端生成的 ID 补进重试请求；
   - 金额是十进制字符串，精度按资产确定；
   - 分页参数 `limit`/`offset` 的范围；
   - 错误响应结构与稳定的 `code`；
   - 两步上传协议，以及未知上传结果时的核对与重试规则。

   每条规则后附来源 ADR 的链接。
2. **重写 `README.md`（≤ 8 KB）**，包含：一句话定位；当前能力一句话，并链接 status；快速开始（本地 Python、Docker Compose、网页开发）；检查命令；依赖更新；文档地图。删除三段 API 叙述，以及“网页票据与证件”“未知上传结果”两段网页行为说明。删除前逐条核对：每条规则都必须能在 api-conventions、ADR 或 OpenAPI 中找到，找不到的就补进 api-conventions。PR 描述中附一张“原位置 → 新位置”对照表。
3. **`overview.md` 去掉所有进度描述**：
   - 删除文件头“本文同时描述现状与目标架构……”那一段；
   - 删除第 1 节“当前工程基础”表格中的状态描述；
   - 删除第 2 节“阶段与状态”列中的进度。

   保留：目标架构、模块边界、业务一致性规则、金额契约，以及文件、OCR、提醒、同步方面的设计约束。
4. **按下面的模板重写 `status.md`（≤ 6 KB）**。删除“已集成基线”一节（历史交给 Git 和 PR）。保留“用户最新指示”这个机制，它是用户暂停和恢复 Codex 的方式。

   ```markdown
   # 当前项目状态
   最后更新：YYYY-MM-DD
   ## 用户最新指示
   - （例如：按优化计划执行 / 暂停 / 继续 T05-3。优先于此前的持续推进授权）
   ## 正在进行
   - 任务：…　分支：…　PR：…
   - 优化计划：见 optimization-plan.md
   ## 任务状态
   | 任务 | 状态 | 一句话说明 |
   ## 已知限制
   - （只写会影响使用或开发判断的事实）
   ## 下一步（最多 5 条，按顺序）
   1. …
   ```
5. **新建 `docs/architecture/decisions/README.md`**：每个 ADR 占一行，写编号、标题、状态（采用/被取代）、一句话结论。今后的新 ADR 统一使用“背景 / 决定 / 影响 / 备选方案”四段模板，不超过 80 行，不写 CI 证据，也不写进度。
6. **`scripts/check_docs.py` 增加两条检查**：
   - (a) Markdown 中不得出现 `/actions/runs/` 链接；
   - (b) 篇幅预算：`README.md` ≤ 8 KB，`docs/engineering/status.md` ≤ 6 KB。
- **不要做**：不改 requirements.md、acceptance.md 的业务内容；不改已合并 ADR 的正文，只在索引中标注状态。
- **验收**：check_docs 通过；仓库 Markdown 中不存在实际 Actions 运行链接（规则示例的 `actions/runs` 字面文本保留）；对照表完整。

### OPT-04 AGENTS.md 分层与开发循环

**Codex 加载 AGENTS.md 的规则（本包据此设计）**：Codex 启动时，从仓库根目录一直到**启动时的工作目录**，逐层合并 AGENTS.md；它**不会**自动加载工作目录以下子目录里的 AGENTS.md。合并后默认上限为 32 KiB，超出部分不会加载。所以根文件要短，并且要明确写出什么时候去读子目录里的文件。

**根 `AGENTS.md`（≤ 8 KB）依次包含：**

1. 项目一句话介绍。
2. **必读文件**只有本文件和 `docs/engineering/status.md`。其余文档按任务类型按需阅读：

   | 任务类型 | 需要读 |
   | --- | --- |
   | 账务、分录、幂等 | `services/api/AGENTS.md`、`api-conventions.md`、ADR 索引中的相关 ADR、acceptance 中的对应案例 |
   | 网页 | `apps/web/AGENTS.md`、`api-conventions.md` |
   | 文件、OCR、备份 | ADR 0012、ADR 0013、`operations.md` |
   | 新业务模块（T06 及以后） | requirements 中的对应条目、overview 第 3–4 节、roadmap 中的对应任务 |
   | 部署、运维 | `operations.md` |
3. **路由规则**（原样写入）：“修改 `services/api/**`、`tests/**` 或 Python 脚本之前，先读并遵守 `services/api/AGENTS.md`；修改 `apps/web/**` 之前，先读并遵守 `apps/web/AGENTS.md`。”
4. **用户指示**：用户的最新指示记录在 `status.md` 的“用户最新指示”中，优先于此前的持续推进授权。用户在当前会话中的指令，又优先于 status.md 中的记录。
5. 不变量：第 1.2 节的精简版。
6. **日常开发循环**：
   1. 读 AGENTS.md 和 status.md，选定任务。开工前执行 `git status`、`git log -5 --oneline`，并查看相关 PR 和 CI。以仓库为准，不凭聊天记录重做（handoff.md 的要点）。
   2. 涉及新的业务规则或数据模型时，先写 ADR；属于产品决定的，开 Draft PR 等用户确认。
   3. 小步实现：每个 PR 建议不超过约 800 行有效改动（不含生成文件、锁文件、快照），一个 PR 最多包含一个迁移；超出时就拆分，并在 PR 中说明。
   4. 运行 `python scripts/check.py`；有一次性数据库时，再运行 `check.py db`。
   5. 在 PR 描述中写验证证据。只有任务或增量的状态变化时，才改 `status.md`。
   6. CI 全部通过后，由用户合并。
7. **完成定义**：通用部分，加上涉及金额时追加的测试矩阵（见下文）。
8. 分支命名与提交前缀：沿用 CONTRIBUTING.md 的约定（`feat:`/`fix:`/`docs:`/`test:`/`refactor:`/`chore:`）。

**`services/api/AGENTS.md` 包含：**

- **金额**：只能经由 `Amount` 和 `AssetDefinition` 处理；入口只接受十进制字符串；写库前必须经过 `prepare_journal_lines`。禁止 float，也禁止依赖 `Decimal` 的默认上下文做运算。
- **新增财务命令时按顺序完成**：
  1. 请求与回执 schema（`kind` 判别联合）；
  2. 服务层，加锁顺序为账本 → 主体 → 排序后的资产；
  3. 分录形状与逐资产配平；
  4. 数据库约束；
  5. 幂等摘要（遵循 OPT-20 之后的规则）；
  6. OpenAPI 快照；
  7. 测试矩阵。
- **测试矩阵**（涉及金额时必须覆盖）：
  - 每种资产的余额守恒；
  - 费用不重复计算；
  - 精度边界：最小单位、最大值、拒绝超精度输入；
  - 同键重放返回原回执，同键不同请求返回 409；
  - 并发的同键请求只入账一次；
  - 任何一步失败都整笔回滚；
  - 账本之间互相隔离；
  - 归档或停用之后，已有回执仍可重放；
  - 备份恢复后重放，任何表都不发生变化。
- **迁移**：使用冻结的 SQL，不 import 应用代码；存在历史数据时必须拒绝降级；新增的索引或约束要同时写进 SQLAlchemy 模型（CI 会运行 `alembic check`）；只能有一个 head。
- **错误**：返回稳定的 `code` 和通用消息；不回显输入，不泄露 SQL。
- **模块放置与后续选型约定**：见第 5 节，原样写入。
- OPT-22 完成后再加一条：新增的业务表必须接入变更日志触发器。

**`apps/web/AGENTS.md` 包含：**

- 金额是字符串，计算用 `money.ts` 里的 BigInt 工具，禁止 `Number`/`parseFloat`。
- 提交结果未知时，走 `PendingCommandController`（`pending-command.ts`）和 `pending-upload.ts`：不重新生成幂等键或上传 ID，原始文件和命令不写入任何浏览器存储。
- 所有界面文本都要同时提供中文和英文（`i18n.ts`）；切换语言不改变业务数据。
- 检查桌面、375px、320px 三种布局；e2e 测试只连接一次性测试服务。
- 提交前运行 `npm run format` 和 `npm run lint`（OPT-06 完成后）。API 类型只能来自 `src/generated/openapi.ts`（OPT-07 完成后）。

**合并与删除**：把 `CONTRIBUTING.md` 和 `docs/engineering/handoff.md` 中仍有效的内容并入根 AGENTS.md，然后删除这两个文件；同步修改 `scripts/check_docs.py` 中的 `required` 列表，以及所有指向这两个文件的链接；在 check_docs 中加入预算检查：根 `AGENTS.md` ≤ 8 KB，三份 AGENTS.md 合计 ≤ 24 KB。

- **验收**：check_docs 通过；根文件包含路由规则；CONTRIBUTING.md 和 handoff.md 已删除；开工必读的两份文件合计 ≤ 14 KB（目前约 81 KB）。

### OPT-05 PR 模板、并行规则、GitHub 设置

- **精简 `.github/pull_request_template.md`**，只保留：
  - 任务编号（T__ / OPT-__）；
  - 改了什么；
  - 明确没有包含什么；
  - 验证（命令 → 结果；没有运行的检查及原因）；
  - 运行影响（迁移、配置、依赖、回滚；不涉及就写“无”）；
  - 两个勾选项：“不含密钥或真实财务数据”；“若任务或增量的状态有变化，已更新 status.md”。
- **把并行规则写进根 AGENTS.md 的“日常开发循环”**：
  - 同一时间最多只有一个含迁移的 PR；
  - `contracts/openapi.json` 和生成的前端类型在 rebase 之后重新生成，不手工合并冲突；
  - `status.md` 只在任务或增量状态发生变化的 PR 中修改。
- **可选**：新增 `.github/dependabot.yml`，只覆盖 `npm`（目录 `/apps/web`）和 `github-actions`，每月一次，分组提交。Python 依赖由 pip-tools 根据 `pyproject.toml` 生成锁文件，Dependabot 处理不好，继续按 README 的流程手动更新。
- **需要用户在 GitHub 网页上手动设置**（Codex 做不了，写进 PR 描述提醒用户）：
  - 给 `main` 设置分支保护，要求 CI 通过才能合并；
  - 合并方式使用 Squash merge；
  - 合并后自动删除分支。
- **验收**：新模板生效；根 AGENTS.md 已包含并行规则。

### OPT-06 前端格式化与 lint

- **现状**：`apps/web/package.json` 没有任何格式化或 lint 工具。`Workspace.tsx` 单行最长 2,072 字符，`PostingForm.tsx` 有 66 行、`DocumentsPanel.tsx` 有 41 行超过 160 字符。
- **时机**：现在最合适。Codex 处于暂停状态，没有打开的网页 PR，一次性格式化不会和其他工作冲突。
- **改动**（按以下三个提交完成）：
  1. **只加工具配置**：
     - 在 devDependencies 中加入 `prettier`、`eslint`、`@eslint/js`、`typescript-eslint`、`eslint-plugin-react-hooks`；
     - 新增 `.prettierrc.json`，内容为 `{ "printWidth": 100 }`，与 ruff 的行宽一致；
     - 新增 `.prettierignore`，忽略 `dist`、`src/generated`、`playwright-report`、`test-results`；
     - 新增 `eslint.config.js`（flat config），启用 `@eslint/js` 推荐规则、`typescript-eslint` 推荐规则，以及 react-hooks 规则：`rules-of-hooks` 设为 error，`exhaustive-deps` 先设为 warn；
     - 在 `package.json` 中增加 `format`、`format:check`、`lint` 三个脚本。
  2. **纯格式化**：只运行 `npm run format`，不做任何手工修改。
  3. **修复不改变行为的 lint 错误**，例如未使用的变量和导入。**不要自动修复 `exhaustive-deps` 警告**：修改依赖数组会改变运行时行为。在 PR 中列出警告数量，留给以后有针对性的 PR 逐个处理。
- **检查**：`check.py web` 增加 `npm run format:check` 和 `npm run lint`。
- **合并后**：在下一个 PR 中，把 `main` 上这次格式化提交的 SHA 写入根目录的 `.git-blame-ignore-revs`。Squash merge 会改变 SHA，所以只能在合并后再写。
- **验收**：`format:check` 通过；lint 没有 error；typecheck、单元测试、e2e 全部通过；格式化提交的 diff 中没有任何非格式改动。

### OPT-07 前端类型由 OpenAPI 生成

- **现状**：`contracts/openapi.json` 由 `scripts/export_openapi.py` 从代码生成，并在 CI 中校验。但 `apps/web/src/ledger-api.ts`、`api.ts`，以及 PR #24 新增的 `document-api.ts`（`DocumentFile`、`UploadReservation`、`UploadCompletion` 等）中的请求和响应类型都是手写的。后端字段一变，前端不会报错。
- **改动**：
  - 在 devDependencies 中加入 `openapi-typescript`，`package.json` 增加脚本 `"gen:api": "openapi-typescript ../../contracts/openapi.json -o src/generated/openapi.ts --default-non-nullable false"`。该选项保留 OpenAPI 未列入 required 的默认字段为可选，不为通过类型检查而补发默认值或改变原请求。
  - 生成的文件提交到仓库。手写类型改为 `type X = components["schemas"]["X"]` 的引用形式，只有界面专用的类型继续手写。
  - `check.py web` 先运行 `gen:api`，再执行 `git diff --exit-code apps/web/src/generated`。CI 中这一步失败，说明契约变了但前端没有同步。
- **注意**：生成类型中的金额字段是 `string`，保持字符串；不引入任何运行时依赖。
- **验收**：typecheck、单元测试、e2e 全部通过；手写的请求和响应类型已经移除。

### OPT-08 模块整理与 documents→files 改名（零行为变化，可拆为 2–3 个 PR）

**为什么要改名**：架构 §4.2 和 T06 的经营单据使用 `Document`/`DocumentLine` 这个名字，而现在后端的 `documents` 模块和前端的 `DocumentsPanel` 其实都是“私有文件”。T05-3 OCR 会大量引用文件模块，不在 OCR 开始前改名，到 T06 就会撞名，改动量也会翻倍。

- **8a 后端：把 `services/api/src/coinpup_api/documents/` 改名为 `files/`**。
  - 表名（`stored_files`、`file_uploads`、`operation_file_links`）和 URL 都不变，**不需要迁移**。
  - 需要更新的引用：`main.py`、`migrations/env.py`、`scripts/check_bundle_restore.py`；相关测试（`test_document_router`、`test_document_schemas`、`test_document_storage`、`test_document_service`、`test_documents_schema`、`test_bundle_archive`），其中测试文件同步改名为 `test_files_*`。
  - 路由 tag `private documents` 可以改为 `private files`，然后重新生成 OpenAPI 快照（只有 tag 变化）。
- **8b 前端：同步改名**。
  - 文件：`src/DocumentsPanel.tsx` → `src/FilesPanel.tsx`，`src/document-api.ts` → `src/files-api.ts`，`src/documents.css` → `src/files.css`，`e2e/documents.spec.ts` → `e2e/files.spec.ts`。
  - 类型：`DocumentFile` → `StoredFile`，`DocumentMediaType` → `FileMediaType`。
  - `Workspace.tsx` 中的视图键 `"documents"` → `"files"`，`documentOperation` → `fileOperation`。
  - 视图键只在代码内部改名；现有 URL 查询参数 `view=documents` 的读写保持不变，保留已有链接的行为。
  - **界面文案（“票据与证件”）不变**。ADR 0013 的文件名和正文不改，只在 ADR 索引中注明“代码中已改名为 files”。
- **8c 拆分 `ledger/posting.py`（950 行）**：
  - `ledger/idempotency.py`：`command_hash`、`_LEGACY_HASH_FIELDS`、`revision_hash`（从 `revisions.py` 移入）、幂等键格式校验、回执读取。
  - `ledger/readers.py`：`_read_operation`、`_read_transfer`、`_read_exchange`、`_read_fees`、`operation_state`。
  - `ledger/commands/`：`classified.py`（期初、收入、支出）、`transfer.py`、`exchange.py`、`fees.py`。
  - `ledger/balances.py`：余额查询。
  - `PostingService` 保留为门面，公共方法签名不变，路由和大部分测试都不需要改。
  - 顺带消除 `posting.py` 方法内部 `from coinpup_api.ledger.revisions import ...` 造成的循环导入。
- **8d 测试目录按源码模块分组**：例如 `tests/unit/{app,auth,ledger,files,ops}/`、`tests/integration/{auth,ledger,files}/`。为避免同名测试文件冲突，二选一：在 `pyproject.toml` 的 pytest 配置中加入 `--import-mode=importlib`，或者给每个测试子目录加 `__init__.py`。确保 `tests/integration/conftest.py` 仍然生效。
- **不要做**：不改包名、表名、URL、响应结构和界面文案；不改业务逻辑；不“顺手”优化其他东西。
- **验收**：全部检查通过，包括 e2e；OpenAPI 快照除 tag 外没有变化；`git diff -M --stat` 显示改动以移动和重命名为主；PR 描述写明零行为变化。

### OPT-09 Python 类型检查（可选）

- 在 dev 依赖中加入 mypy（纯 Python，CI 中安装简单），使用非严格配置，只检查 `services/api/src`，并纳入 `check.py fast`。
- 现有报错要么逐个修复，要么按模块暂时忽略；新代码必须通过检查。
- 修改依赖声明后，按 README 的流程重新生成两份锁文件。
- **验收**：`check.py fast` 包含类型检查，并且通过。

### OPT-10 读余额不加行锁

- **现状**：`PostingService.balances()` 调用 `self._locked_ledger(...)`，对账本和主体执行 `SELECT … FOR UPDATE`，导致纯读查询也会和写入互相等待。其他读接口（`get_operation`、`list_operations`）用的是不加锁的 `self._ledger(...)`。
- **改动**：
  - 改用 `self._ledger(session, owner_id, ledger_id)`。
  - 给 `_transaction` 增加一个只读参数，把这次读事务设为 `REPEATABLE READ, READ ONLY`，保证“账户资产列表”和“合计”两次查询来自同一个快照。
- **测试**：新增集成测试。连接 A 开启写事务、持有账本行锁、暂不提交；此时连接 B 调用余额接口，应在短超时内返回，并且结果与改动前一致。
- **验收**：现有余额测试全部通过；新增的“不阻塞”测试通过。

### OPT-11 余额与日期查询索引

- **现状**：余额是对 `journal_lines` 全量求和得到的（`_current_units`、`balances`），但这张表只有 `journal_id` 和 `ledger_id` 两个单列索引。
- **改动**：新增一个迁移：

  ```sql
  CREATE INDEX ix_journal_lines_account_balance
      ON journal_lines (ledger_id, account_id, asset_id) INCLUDE (amount)
      WHERE role = 'account';
  CREATE INDEX ix_journals_ledger_transaction_date ON journals (ledger_id, transaction_date);
  CREATE INDEX ix_journals_ledger_recognition_date ON journals (ledger_id, recognition_date);
  ```

  同时在 `JournalLine.__table_args__` 和 `Journal.__table_args__` 中声明相同的 `Index(...)`，部分索引要带上 `postgresql_where` 和 `postgresql_include`。否则 CI 的 `alembic check` 会报模型与数据库不一致。降级时直接删除这些索引，不涉及数据。
- **验收**：
  - 迁移往返通过，`alembic check` 通过；
  - 在 PR 中贴出测试库上的 `EXPLAIN` 输出，证明余额查询能用上新索引。数据量小的时候，可以先执行 `SET enable_seqscan = off`。

### OPT-12 流水列表批量读取与按日期浏览

- **现状**：`list_operations` 对每一条业务都调用 `operation_state` → `_read_operation`，分别去取凭证、取行、取资产；已取消的业务还要再查一次冲销凭证。一页 200 条要发出数百次查询。排序固定为 `created_at DESC`，既不能按交易日浏览，也不能按日期筛选。
- **改动**：
  1. **批量读取**：一次查出本页所有 current journal、所有行（`journal_id IN …`）、涉及的资产，以及已取消业务的冲销凭证，再用和现在相同的纯函数组装响应。单条读取也走同一条路径。
  2. **新增可选查询参数**：
     - `order=created_at|transaction_date`，默认 `created_at`，保持现有行为；
     - `from_date`、`to_date`，按当前入账的交易日筛选，闭区间。
     
     按交易日排序时，同一天内的稳定次序为 `transaction_date DESC, created_at DESC, id DESC`。
  3. 网页流水改为使用 `order=transaction_date`；同步更新 OpenAPI 快照和生成的前端类型。
- **测试**：
  - **响应等价**：用同一批虚构数据，在默认参数下，改动前后 `GET /operations` 返回的 JSON 完全相同。
  - **查询次数**：用 SQLAlchemy 的 `before_cursor_execute` 事件计数，一页 200 条时查询次数为常数（≤ 8）。
  - 日期筛选与排序都正确。
- **验收**：以上测试通过；网页流水按交易日显示。

### OPT-20 幂等摘要 v2（T05-4 之前完成）

- **现状与风险（已核实）**：
  - `command_hash` 只对 `opening`/`income`/`expense`/`transfer` 使用冻结字段集合 `_LEGACY_HASH_FIELDS`，**`exchange` 没有冻结**。
  - `revision_hash` 直接对 `payload.model_dump(mode="json")` 求摘要，而 `*Replacement` 继承自 `*Create`。
  - **后果**：今后只要给 `ExchangeCreate` 或任何一个 `*Create` 加上带默认值的可选字段，旧回执重放时算出的摘要就会变化。改 `*Create` 还会经由 Replacement 波及所有历史更正。本应安全重放的重试会得到 409 `idempotency_conflict`。
  - T05-4 的“人工确认入账”很可能给收支命令加上来源字段（例如 `source_file_id`、`draft_id`），T06 还会加 `party_id`、`document_id` 等字段，正好会触发这个问题。
- **方案**：先写 ADR。这是技术决定，不需要用户确认。
  - `command_receipts` 新增 `hash_version smallint NOT NULL DEFAULT 1`。
  - **v1** 就是现有算法，原样冻结，只用来校验已有回执，今后永不修改。
  - **v2**：取**收到的原始 JSON 请求体**（只解析，不经过 Pydantic 补默认值）做规范化（键排序、紧凑分隔、`allow_nan=False`），再连同 action、路由模板、`ledger_id`（更正和取消另加 `operation_id`）一起求 SHA-256。新回执一律写 v2。
  - 重放时，按回执上记录的 `hash_version` 选择对应算法计算并比较。
  - **实现提示**：在路由层用一个 async 依赖读取 `await request.body()`（Starlette 会缓存请求体，不影响 Pydantic 解析），传给服务层；业务执行仍然使用 Pydantic 校验后的模型。
  - **把语义变化写进 `api-conventions.md`**：在 v2 下，省略字段与显式的 null 或空数组属于不同的请求体（v1 中“省略 `fees` 与 `[]` 等价”的规则只对旧回执保留）。客户端重试时必须发送原请求体，网页的 `PendingCommandController` 已经做到了这一点。
- **测试**：
  - 所有现有回执夹具（四类旧命令、换汇、更正、取消）在升级后仍能重放，并返回原回执；
  - 在测试中用子类模型模拟“给 `ExchangeCreate`/`IncomeCreate` 加一个带默认值的字段”，v2 回执仍能成功重放；
  - 同键不同请求仍返回 409；`"1.0"` 和 `"1.00"` 仍视为不同请求。
- **验收**：以上测试全部通过；ADR 已合入；api-conventions 已更新。

### OPT-21 数据库形状校验按业务类型分发（T06 之前完成）

- **现状**：每种业务的分录形状都写在 PL/pgSQL 大函数的 `IF operation.kind = ... ELSIF ...` 分支里。迁移 0005、0006、0007 都对 `coinpup_validate_initial_journal` 做了整体的 `CREATE OR REPLACE`，0007 中的 `coinpup_validate_posting_shape` 又复制了一份同样的分支逻辑。按这个模式，T06/T07 每新增一种业务，都要重写整段函数。
- **改动**：新增一个迁移，**语义必须与现状完全一致**：
  - 新建通用不变量函数 `coinpup_validate_journal_common(journal uuid)`，检查行数 ≥ 2、逐资产配平、逐组成部分配平、费用组成部分的形状、归属一致。
  - 每种业务一个函数：`coinpup_validate_shape_opening`、`_income`、`_expense`、`_transfer`、`_exchange`。
  - `coinpup_validate_posting_shape` 只负责分发：先调用通用函数，再按 `kind` 调用对应的函数；遇到未知的 kind 就报错。
  - `coinpup_validate_initial_journal` 改为调用 `coinpup_validate_posting_shape`，不再复制分支逻辑。
  - 这样以后每新增一种业务，只需新增一个 `coinpup_validate_shape_<kind>`，再改几行分发函数。
  - 降级时恢复 0007 的原函数体（冻结 SQL）。
- **不要做**：不要把形状校验移出数据库。数据库约束是应用出 bug 时的最后一道防线。
- **测试**：现有的 `tests/integration/test_*_constraints.py` 不做任何修改即可全部通过；错误码和约束名（例如 `ck_journal_shape`）保持不变。
- **验收**：集成测试全部通过；迁移往返通过。

### OPT-22 变更日志与同步游标（T06 之前完成）

- **依据**：需求 P-08 要求在第一阶段就保留“修改游标及删除标记”，但目前仓库中没有全局递增的变更序号。T05-3 以后还会陆续新增任务、草稿、经营单据等十几张表，先建立统一机制，就不用以后逐表补做。
- **方案**：先写 ADR。这是技术决定，不需要用户确认。
  - **新表 `change_log`**：字段为 `seq bigint identity`、`owner_id`、`ledger_id`（全局对象可以为空）、`entity_type`、`entity_id`、`entity_version`、`change_kind`（`upsert`/`archive`/`restore`/`cancel`）、`changed_at`。只能追加，禁止 UPDATE/DELETE，沿用现有封存触发器的写法。
  - **由 AFTER INSERT/UPDATE 触发器写入**，保证不会漏写。覆盖的表：`entities`、`ledgers`、`accounts`、`account_assets`、`categories`、`assets`、`financial_operations`、`stored_files`、`operation_file_links`。
  - **提交顺序陷阱（必须处理）**：序列号按分配顺序可见，而不是按提交顺序。比如事务 T1 拿到 seq=10，T2 拿到 seq=11；T2 先提交，客户端读到 11 后把游标推进到 11；T1 随后才提交，seq=10 就永远不会被读到。二选一：
    - **推荐（简单）**：写变更前先获取 `pg_advisory_xact_lock(<所有者常量键>)`，让写事务按提交顺序拿到序列号。单用户场景下这个代价可以忽略。服务层在每个写事务开始时、在获取任何业务锁之前先取这把锁，触发器里再取一次作为保险（同一会话可以重入）。
    - **无锁方案**：记录 `pg_current_xact_id()`（xid8）；读取时只返回 `txid < pg_snapshot_xmin(pg_current_snapshot())` 的行，游标改为 `(txid, seq)`，并按这个顺序分页。
  - **读取接口**：`GET /api/v1/changes?after=<cursor>&limit=`，返回变更行和新的游标。首次同步时，先用现有列表接口取全量数据，再从当时的游标开始增量同步。
  - **删除标记**：系统不做物理删除。归档和取消就是 tombstone，由 `change_kind` 表达。
  - `change_log` 要纳入备份 bundle，恢复后游标继续有效。
- **测试**：
  - 并发乱序提交时不漏读（用两个连接构造“先分配、后提交”的场景）；
  - 每张受跟踪的表在插入和更新时都会产生变更行；
  - 游标分页既不重复也不遗漏；
  - 恢复后可以继续增量同步。
- **验收**：测试通过；ADR 已合入；`services/api/AGENTS.md` 已加入“新业务表必须接入变更日志”的规则。

### OPT-23 🛑 资产负债类账户与维度（T06/T07 的前置）

- **问题**：
  - 分录行的角色是一个封闭集合（`ck_journal_lines_role`：`account`/`income`/`expense`/`equity`/`exchange`）；
  - 分类只有收入和支出两种（`ck_categories_kind`）；
  - `accounts.kind` 只有资金账户类型。

  T06 的应收应付、预收预付，以及 T07 的“公司欠个人”，都是需要长期累计余额的资产或负债，现有结构里没有地方放。如果每个新业务都加一个 role，校验和读取逻辑会持续膨胀。
- **产出**：ADR 草稿（Draft PR，不写实现代码），内容包括：
  1. **选项 A：把账户扩展为“带类别的账户”**。`accounts` 增加 `account_class` 字段：`money` 表示现有的资金账户，另外新增由系统管理的 `receivable`、`payable`、`intercompany`、`advance` 等类别。分录行增加可选维度：`party_id`（客户或供应商）、`counterparty_entity_id`（往来主体）、`document_id`/`document_line_id`。某个往来单位的应收余额，就是这个维度上所有行的合计。分类仍然只表达损益。
  2. **选项 B**：账户仍然只表示资金，新增 role（`receivable`、`payable`、`intercompany`），并在行上挂维度。
  3. **推荐方案及理由**。建议选 A：资产负债与损益分开，余额计算统一为“按账户求和”，报表和同步都更简单。
  4. **对现有数据的影响**：现有账户全部归为 `money`，现有分录不改写；说明系统账户的 `account_assets` 关联怎么处理（自动关联，或者放宽约束）。
  5. **用分录逐行演示以下验收案例**（使用虚构数据）：
     - 300 USD 的供应商账单分 100 和 50 两次支付：经营费用 300，已付 150，未付 150；
     - 9 月归属的账单 10 月才付款：经营视角记在 9 月，现金视角记在 10 月；
     - 个人替公司支付 100 CNY：公司费用 100，公司欠个人 100，个人费用为 0；公司偿还后往来结清，不再产生费用；合并视角下对外费用为 100；
     - 收到客户预付款，之后开票冲抵。
  6. 对 API、迁移、余额接口（资金余额与往来余额分开展示）、网页的影响清单。
- 用户确认后，再拆分成实现 PR。

### OPT-24 🛑 跨账本关联业务（T07 的前置）

- **问题**：锁、幂等回执（`command_receipts` 的主键是 `(ledger_id, key)`）、复合外键都限定在单个账本内。但代付需要在一个事务里同时写个人账本和公司账本。
- **ADR 草稿需要回答**：
  1. **关联对象**：例如新表 `operation_groups(id, owner_id, kind, status, version)`。每个参与账本各有一条 `FinancialOperation`，各自逐资产配平，通过 `group_id` 关联起来。
  2. **加锁顺序**：所有参与账本按 UUID 升序加锁，然后锁各自的主体，再锁排序后的资产；所有锁都在第一次写入之前取得。如果 OPT-22 采用了咨询锁，咨询锁排在最前面。
  3. **幂等作用域**：跨账本命令的回执放在所有者级别（新表或扩展主键），与单账本回执互不冲突。
  4. **更正与取消**：整组原子冲销，不允许只改其中一边。
  5. **归档**：任一参与主体被归档时，拒绝新的组命令；已有的回执仍然可以重放。
  6. **死锁测试计划**：两个方向相反的跨账本命令同时执行。
- 依赖 OPT-23 中往来账户的设计。

### OPT-25 🛑 结账与期间锁定（T08/T09 之前）

- **问题**：按照 ADR 0008，更正时冲销凭证沿用原来的交易日和归属日。所以已经出过报表、甚至已经报过税的期间，数字可能被事后改掉，而系统不会给出任何提示或限制。
- **ADR 草稿需要让用户选择**：
  1. 每个账本设一个 `closed_through` 日期，带版本，可以调整，调整时记录原因。
  2. 新入账的交易日或归属日 ≤ `closed_through` 时，是拒绝还是只给警告？
  3. 更正或取消涉及已结账期间的业务时，是拒绝（必须先“重新打开”期间并填写原因），还是允许但把冲销和替代分录记在当前开放的期间？
  4. 报表上如何标注“已结账”和“未结账”。
- **推荐**：拒绝，并允许带原因地重新打开期间。单用户下这种方式最简单，审计也清晰。

### OPT-30 🛑 登录防护与暴露面（部署到服务器之前）

- **现状**：登录失败计数保存在一行全局记录里（`auth.py` 的 `lock_login_guard`），默认 15 分钟内失败 5 次就全局锁定 15 分钟。服务一旦暴露到公网，任何人都可以持续让所有者无法重新登录（已登录的会话不受影响）。目前也没有第二因素。
- **ADR 草稿给出组合方案，请用户选择**：
  1. **按来源计数**：只有配置了可信反向代理时，才解析 `X-Forwarded-For`（新增配置，例如 `COINPUP_TRUSTED_PROXIES`）；按来源 IP 限流，同时调高全局阈值作为兜底。
  2. **TOTP 第二因素**（可选启用）：通过服务器 CLI 绑定和重置，恢复码只显示一次；登录接口增加验证码字段。
  3. **把部署建议写进 `operations.md`**：优先只通过 VPN、Tailscale 或 WireGuard 访问，不直接暴露在公网上。
- **推荐**：1 和 3 必做；2 建议启用，因为系统里保存着公司证件和财务数据。

---

## 5. 模块放置与后续选型约定

本节不需要立即执行。在 OPT-04 中原样写入 `services/api/AGENTS.md`，供后续任务遵守。

| 路线图任务 | 放置位置 |
| --- | --- |
| T05-3 OCR | `coinpup_api/ocr/`，独立进程入口 `python -m coinpup_api.ocr.worker` |
| T05-4 待确认草稿 | `coinpup_api/ocr/`（草稿与确认），确认入账时调用现有的财务命令服务 |
| T06 经营单据 | `coinpup_api/business/`（往来单位、项目、单据与单据行、Invoice） |
| T07 代付报销 | `coinpup_api/settlement/` |
| T08 汇总估值 | `coinpup_api/reporting/`（只读查询）和 `coinpup_api/quotes/` |
| T09 提醒 | `coinpup_api/reminders/` |
| T11 同步 | `coinpup_api/sync/` |
| T12 App | `apps/mobile/`（Flutter） |

- **OCR**：与 API 共用同一个 Python 包。OCR 的重依赖放进 `pyproject.toml` 的可选依赖组 `ocr`；Dockerfile 增加一个 `worker` 构建目标，API 镜像不安装 OCR 依赖。任务队列用 PostgreSQL 表加 `SELECT … FOR UPDATE SKIP LOCKED` 实现，任务和业务数据在同一个库里，自然会进入备份 bundle。不引入 Redis 或 Celery。识别结果只生成草稿，必须经用户确认后才调用财务命令入账。
- **定时任务**（行情、周期 Invoice 草稿、提醒、备份）：由 `coinpup_api.jobs` 提供 CLI 子命令，用 Compose 中一个简单的调度容器或宿主机 cron 调用；每个任务用数据库唯一键保证幂等。
- **行情**：`quotes` 表记录来源、币对、有效时间、获取时间。估值只做只读计算，永远不改写分录。
- **邮件**：使用标准库 `smtplib`；投递记录表以（事件、渠道、计划时间）为唯一键，防止重复发送。
- **报表**：先用 SQL 查询或视图实时计算，实测变慢之后再考虑快照或物化视图。
- **移动端**：API 类型同样由 `contracts/openapi.json` 生成。

---

## 6. 与路线图的衔接，以及计划的收尾

T05-1 和 T05-2 已经完成（PR #23、#24）。路线图的下一步是 T05-3。

| 路线图任务 | 开始前应完成 |
| --- | --- |
| T05-3 OCR worker | OPT-01 至 OPT-08；第 5 节约定已写入 `services/api/AGENTS.md` |
| T05-4 待确认草稿与人工确认 | OPT-20（确认入账很可能给收支命令增加来源字段）；如果新增业务类型，还需要 OPT-21 |
| T06 经营单据 | OPT-21、OPT-22；OPT-23 已确认并实现 |
| T07 代付报销 | OPT-24 已确认并实现 |
| T08 汇总估值 | OPT-11、OPT-12；OPT-25 已确认并实现 |
| T10 部署 | OPT-30 已确认并实现 |
| T11 同步 | OPT-22 |

**所有工作包通用的完成定义：**

- 相关检查全部通过，PR 描述写明执行的命令和结果。
- 承诺不变的行为要有测试证明确实没变，重构类包尤其如此：响应 JSON、回执重放、OpenAPI 快照。
- 涉及金额、分录或幂等时，覆盖 `services/api/AGENTS.md` 中的测试矩阵。
- 新增或修改接口、迁移、配置时，同步更新 `api-conventions.md`、`operations.md` 或 `.env.example`。
- 本文件的状态表已更新。

**全部工作包完成后**：把仍然有效的规则并入对应的 AGENTS.md；删除本文件，以及 AGENTS.md 中指向它的那一行；在 `status.md` 中记录一行“优化计划已完成”。
