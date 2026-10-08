# 应用运行与数据库/私有文件恢复

## 会话与部署配置

管理员由服务器 CLI 初始化或重置；容器密码重置命令为 `docker compose exec api python -m coinpup_api.admin reset-password`，交互输入新密码并撤销全部旧会话。初始化密码长度为 12–128 字符，不提供默认凭据。设计依据见 [ADR 0002](../architecture/decisions/0002-administrator-session.md)。

`COINPUP_ALLOWED_ORIGINS` 是 JSON 格式的精确 scheme/host/port 列表，开发来源示例见 [.env.example](../../.env.example)。生产模式须显式设置 HTTPS 来源，并核对反向代理、TLS 与 Cookie 的 Secure 行为。默认登录限制为 15 分钟内 5 次失败后限制 15 分钟，会话默认 12 小时；可配置项以 [Settings](../../services/api/src/coinpup_api/config.py) 为准。来源、CSRF 和会话不是可互相替代的检查，见 [API 约定](../architecture/api-conventions.md)。

## 可选文字解析环境

默认 API 使用 `requirements.lock`，开发检查使用 `requirements-dev.lock`；PDF 依赖另锁在 `requirements-ocr.lock`，受默认锁约束并包含安装哈希。安装可选组不会自动启用 OCR 任务；固定选型及人工复核策略见 [ADR 0020](../architecture/decisions/0020-ocr-prefill-and-review.md)。

```sh
python -m pip install -r requirements-dev.lock
python -m pip install --require-hashes -r requirements-ocr.lock
python scripts/check.py ocr
```

默认 `fast` 保留全部普通测试；可选库由独立 OCR CI 实际安装检查，不用 `importorskip` 冒充验收。依赖和许可证登记见[可选解析依赖](ocr-dependencies.md)。Linux helper 固定 Python/处理入口，先设资源限制再导入解析库；目录/文件分别0700/0600，不继承数据库、会话或代理环境，stderr 不进日志。地址空间不是 RSS 上限，单文件限制不是临时目录总量；容器约束如下，helper 本身不是网络/文件系统安全沙箱。

## 显式启用 Linux OCR worker

仅支持已验证的 Linux amd64 路径。原生引擎复用固定来源构建的 Tesseract 5.5.3/Leptonica，生产 worker 只复制原生安装目录，另安装带哈希的 OCR 锁；不含 Paddle 或编译工具，默认 API 构建不安装 OCR。准备构建输入须联网，运行时不下载模型。以下只构建，不运行质量基准或正式验收：

```sh
python -m scripts.ocr_benchmark.engine_assets fetch --output-dir build/engine-assets
python ops/ocr-benchmark/build_inputs.py fetch --output-dir build/engine-build-inputs
mkdir -p build/paddle-wheelhouse
cp build/engine-assets/wheels/*.whl build/paddle-wheelhouse/
python -m pip download --require-hashes --only-binary=:all: --find-links build/paddle-wheelhouse -r ops/ocr-benchmark/paddle.lock -r ops/ocr-benchmark/bootstrap.lock --dest build/paddle-wheelhouse
chmod -R a+rX build/engine-assets build/engine-build-inputs build/paddle-wheelhouse
docker build --target tesseract -f ops/ocr-benchmark/Dockerfile -t coinpup-ocr-tesseract:local .
docker compose -f compose.yaml -f compose.ocr.yaml build
```

使用[OCR覆盖配置](../../compose.ocr.yaml)前先按既有流程启动数据库/API、迁移并创建管理员，随后 `docker compose -f compose.yaml -f compose.ocr.yaml up -d worker`。API 与 worker 必须构建自同一提交；同时以该覆盖配置重建 API，才会显式开启新任务。单独部署时设置 `COINPUP_OCR_ENABLED=true`，默认为 false；关闭只阻止新意图，既有意图仍可读取/重放，暂停计算还需停止 worker。

worker 非root、根文件系统与原件卷只读、无 capabilities/新增权限；2 CPU、4 GiB、64进程及512 MiB私有 tmpfs。内部 Docker 网络只连 PostgreSQL，无公网出口和发布端口；父进程需要数据库连接，不能把整个 worker 称为完全禁网。实际引擎额外通过 network=none 容器验收。模型及 manifest 内置只读、逐字节校验，保留原许可；运行失败不在线补下载。长期恢复还需保存同一提交的 API/worker 镜像与原生构建来源。

`python -m coinpup_api.ocr.worker --once` 只尝试领取一次；默认串行轮询。清理失败/数据库提交不确定时进程退出，容器重启后靠新租约恢复；归档、旧 token 和过期结果不能写回。升级及恢复核对时先停止 worker，确认原件/数据库一致后再启动。完整可执行例子与虚构容器验收见 [OCR工作流](../../.github/workflows/ocr.yml)，不会读取 GitHub secrets。

## 备份范围

余额与交易日/归属日的查询索引通过显式 Alembic 迁移建立，模型定义见 [账务模型](../../services/api/src/coinpup_api/ledger/models.py)。普通建索引在迁移事务中执行，安排维护窗口；降级只删除本次新增索引，不改财务行或回执。测试中的 `enable_seqscan=off` 仅验证索引适用性，不是部署设置，也不代表实际性能提升。

财务回执的 `hash_version` 随数据库一起备份。升级到摘要 v2 后，存在任何 v2 回执就拒绝降级到无版本字段的结构；不得改写或删除回执来规避保护。旧 v1 回执升级后保持原响应与重放规则，见 [ADR 0014](../architecture/decisions/0014-versioned-command-hashes.md)。

**`backup_database.py` 始终只备份数据库；有原件时使用 `backup_bundle.py`。** 两者都不包含配置、密钥、PostgreSQL 角色或整个服务器；自动定时、异地副本、保留策略与生产演练属于 T10。

## 包含票据与证件的 bundle

`backup_bundle.py` 和 `restore_bundle.py` 使用以下已有 POSIX、凭据、隔离空数据库要求。文件根必须是部署设置的 `COINPUP_FILES_DIRECTORY`；Compose 默认为 `/app/data/files`，存于 `files_data` 命名卷。该目录不得对外静态托管。原件只做格式签名检查，尚未代表 OCR 成功。

在已通过私密环境注入 `COINPUP_BACKUP_DATABASE_URL` 后，主机备份示例：

```sh
python scripts/backup_bundle.py --storage-root data/files --output backups/bundle-20261004-01
```

源文件根与输出父目录必须已存在，输出目录必须不存在。零原件允许源根为空，不要求先建 `blobs`。备份在同一 PostgreSQL 17 只读快照中取得完整数据库和原件清单，随后复制全部被引用原件（含归档）并验证 SHA-256/长度；不包含 staging、孤立 blob 或快照后才提交的上传。备份期间正常上传可以继续，暂未启用原件垃圾回收。

Compose 中先按下面通用说明创建 UID 10001 私有 backups 目录，再运行：

```sh
docker compose run --rm --no-deps -v "$PWD/backups:/backups" -e COINPUP_BACKUP_DATABASE_URL api python scripts/backup_bundle.py --storage-root /app/data/files --output /backups/bundle-20261004-01
```

成功目录包含 `database.dump`、`blobs/` 和最后发布的 v2 `manifest.json`。manifest 保存精确文件行、摘要和大小，限制 64 MiB；超过时失败，不能算完成。SHA-256 检测损坏，不提供加密、签名或可信来源证明。未完成目录留待检查，重试使用新输出目录。

恢复前创建符合下文规则的新空隔离库，并注入 `COINPUP_RESTORE_DATABASE_URL`。文件目标必须是**不存在的新目录**，即使已有空目录也拒绝；父目录预先创建并限制访问。主机示例：

```sh
python scripts/restore_bundle.py --backup backups/bundle-20261004-01 --storage-root recovery/files-20261004-01 --confirm-empty-database coinpup_restore_20261004
```

Compose 恢复需要私有 recovery 父目录，备份只读挂载，文件写入新的独立恢复目录：

```sh
sudo install -d -m 700 -o 10001 -g 10001 recovery
docker compose run --rm --no-deps -v "$PWD/backups:/backups:ro" -v "$PWD/recovery:/recovery" -e COINPUP_RESTORE_DATABASE_URL api python scripts/restore_bundle.py --backup /backups/bundle-20261004-01 --storage-root /recovery/files-20261004-01 --confirm-empty-database coinpup_restore_20261004
```

恢复先校验 manifest、dump 和全部常规原件（拒绝 symlink、缺失、损坏和未列入清单的文件），再检查目标空库、复制字节、单事务导入，最后精确比较全部 `stored_files` 行。缺失或损坏不以空白文件替代。失败不自动删除目录或数据库；不得直接重用失败目标。成功后仍需在隔离应用核对全部账务和关联，手动切换数据库与文件根；脚本不会修改运行中的应用配置。

CI 的 `check_compose_backup.py` 先验证旧数据库单独恢复，再执行 `check_bundle_restore.py`：两个账本同内容独立、同账本去重、PDF/PNG、归档文件/关联、取消流水、未完成上传、完整表与原件字节一致、恢复后原回执重放。演练在导出快照后、pg_dump 前提交一次新上传，确认晚到记录与 staging/孤立 blob 都不进入备份。

OCR 任务、冻结配置、候选证据、人工编辑和提交有序日志与其他业务表一起进入数据库快照；备份不执行识别，也不会把任务改成成功。引擎、模型和语言制品不属于原件卷，应另存固定版本、许可证及摘要，恢复后必须匹配任务的冻结处理配置，不能套用新默认值。

恢复核对期间停止 worker，旧实例及其租约不能连接新目标。核对全部行及原件后启动新实例：已完成任务只读，原完成重放不产生新行；过期 running 经数据库时钟重新领取，增加 generation 并换 token，旧 token 的续租、失败及完成均被拒绝。识别计算与原件读取始终在领取事务提交之后，不持有数据库业务锁；只有短状态/草稿事务按咨询锁→账本→主体的顺序写入。

## 运行环境与凭据

- 使用 Linux/POSIX 主机或 Linux 容器、项目 Python 依赖和 PostgreSQL 17 的 `pg_dump`、`pg_restore`。与当前 PostgreSQL 17 服务使用相同主版本工具；旧版工具或失败退出都不能作为成功备份。
- Windows 原生执行会明确拒绝：Python 文件模式不能保证 Windows ACL 的私有性。Windows 通过 Docker Desktop 的 Linux 容器运行，并使用受限的 Linux 卷或 WSL 文件系统保存备份；Windows 共享挂载的 ACL 需另外管理。
- 单独从环境提供 `COINPUP_BACKUP_DATABASE_URL`（源）和 `COINPUP_RESTORE_DATABASE_URL`（恢复目标）。脚本**不自动读取 `.env`**，也不回退到应用数据库。支持 `postgresql://` 或 `postgresql+psycopg://`，必须明确主机、用户名、密码和库名，密码特殊字符需 URL 编码；当前仅支持 `sslmode` 查询参数。
- 用部署平台的私密环境配置或 `read -r -s` 输入 URL；不要把真实 URL 写在命令参数、Shell 历史、工单或仓库中。工具不打印 URL、密码、数据库原始报错或数据；密码通过临时 `0700` 目录中的 `0600` 密码文件交给 PostgreSQL 工具，不进入参数或 `PGPASSWORD`。
- 备份目录创建为 `0700`，dump 和 manifest 为 `0600`。由执行用户持有，父目录也应由运维用户控制。SHA-256 用于检测损坏，不是加密或签名；仅恢复可信来源，单独安全保存备份。

## 主机备份

从仓库根目录，在已安装项目依赖的 Python 环境运行。下面的交互输入在 Bash 中隐藏内容；也可由部署平台预先注入同名环境变量。

```sh
read -r -s -p 'Source PostgreSQL URL: ' COINPUP_BACKUP_DATABASE_URL
export COINPUP_BACKUP_DATABASE_URL
mkdir -m 700 -p backups
python scripts/backup_database.py --output backups/manual-20261003-01
unset COINPUP_BACKUP_DATABASE_URL
```

`--output` 必须是不存在的新目录，父目录必须已存在；存在时直接失败，不覆盖。成功产生 `database.dump`（custom 格式）和 `manifest.json`（版本、UTC 创建时间、大小、SHA-256、database-only 范围）。manifest 是完成标记；工具失败留下的无 manifest 目录不算成功，不应拿去恢复。失败后检查运行权限、连接和客户端版本，重试使用新的输出目录。

## Compose 备份

项目 API 镜像包含 PostgreSQL 客户端及 `scripts/`，运行用户 UID 为 `10001`。Linux 主机先由运维人员创建只允许该用户访问的备份目录，再挂入容器。下面命令中的输出名每次更换。

```sh
sudo install -d -m 700 -o 10001 -g 10001 backups
read -r -s -p 'Source URL (host postgres): ' COINPUP_BACKUP_DATABASE_URL
export COINPUP_BACKUP_DATABASE_URL
docker compose run --rm --no-deps -v "$PWD/backups:/backups" -e COINPUP_BACKUP_DATABASE_URL api python scripts/backup_database.py --output /backups/manual-20261003-01
unset COINPUP_BACKUP_DATABASE_URL
```

数据库容器应已启动；容器内 URL 的主机为 `postgres`，不是主机 Python 常用的 `localhost`。`-e` 只传变量名，不把值放进 Docker 命令参数。受限目录不能通过 `chmod 777` 解决权限问题。

## 恢复到新建的空数据库

1. 由数据库管理员单独创建隔离库，建议使用 `TEMPLATE template0`，例如 `coinpup_restore_20261003`。将该库所有权赋给恢复账户，并保持应用和其他客户端断开；不要先运行 Alembic，迁移表也会让目标成为非空。
2. 将目标连接 URL 注入 `COINPUP_RESTORE_DATABASE_URL`。库名必须满足 `coinpup_restore_<小写字母、数字或下划线>`，确认参数必须与实际连接库名完全一致。该命名限制专门用于防止把日常应用库直接作为恢复目标。
3. 运行恢复；成功后在隔离环境核对管理员、登录会话、迁移版本、主体归属、财务流水、原币余额及幂等回执。切换应用连接是独立人工操作，此脚本不执行切换、不移除旧库。

主机命令：

```sh
read -r -s -p 'Empty disposable target PostgreSQL URL: ' COINPUP_RESTORE_DATABASE_URL
export COINPUP_RESTORE_DATABASE_URL
python scripts/restore_database.py --backup backups/manual-20261003-01 --confirm-empty-database coinpup_restore_20261003
unset COINPUP_RESTORE_DATABASE_URL
```

Compose 同样使用目标 URL 中的主机 `postgres`，备份卷只读挂载：

```sh
docker compose run --rm --no-deps -v "$PWD/backups:/backups:ro" -e COINPUP_RESTORE_DATABASE_URL api python scripts/restore_database.py --backup /backups/manual-20261003-01 --confirm-empty-database coinpup_restore_20261003
```

恢复先校验 manifest、custom 格式文件头、文件大小和 SHA-256，再执行 `pg_restore --list` 验证可读取性。目标检查包含实际库名、其他连接、用户 schema、表/视图/序列、函数、类型、扩展、large objects、事件触发器、复制对象及外部数据对象。允许 PostgreSQL 初始化提供的空 `public` schema 和 `plpgsql`；存在应用表时直接拒绝，空表也不例外。

恢复使用单事务及 `--exit-on-error`，任何 PostgreSQL 工具非零退出都会使命令失败；不使用 `--clean`、`--create`、DROP、TRUNCATE，也不自动删除失败输出。源对象的 owner/ACL 不迁移，恢复对象属于目标账户；恢复账号和运行账号的权限由运维人员单独核对。操作期间持有 advisory lock 防止两个 Coinpup 恢复互相竞态；这不阻止其他客户端连接，隔离目标并保持其他写入者断开仍是运行前提。

恢复后的会话记录与备份时一致，尚未过期、未撤销的原会话可能仍可使用。若实际切换时需要全部退出，可使用管理员密码重置流程撤销会话；不要把恢复演练自动变成生产密码重置。

## 自动化验证与验收边界

单元检查不需要 PostgreSQL：

```sh
python -m pytest tests/unit/ops/test_backup_restore.py tests/unit/ops/test_backup_snapshot.py
```

这些检查覆盖现有备份不得覆盖、损坏备份拒绝、非空/错误/有其他连接的目标拒绝、工具失败传播、凭据不进入参数与环境、精确文本快照、封存探测始终回滚，以及 POSIX 私有文件权限。Windows 单测跳过实际 POSIX 权限检查，不能把跳过写成已验收。

真实 CI 演练必须使用全新、一次性的 PostgreSQL 17 服务，先安装项目（例如 `pip install -e .`）并迁移源库。设置：

| 环境变量 | 内容 |
| --- | --- |
| `COINPUP_RUN_BACKUP_TESTS` | 必须为 `1`，显式允许建立测试管理员和新库 |
| `COINPUP_BACKUP_TEST_SOURCE_URL` | 已迁移、无管理员的专用测试源库 URL |
| `COINPUP_BACKUP_TEST_TARGET_URL` | 同一测试服务器、同一用户，库名固定 `coinpup_restore_test` 的 URL |

```sh
python scripts/check_backup_restore.py
```

checker 拒绝已存在的目标库和已有管理员的源库；只执行创建新库，不清空已有库。它创建随机测试密码、真实会话及明确标注的虚构业务数据，备份并恢复后比较 `public` 中全部非扩展所属应用表，包括认证、迁移、结构、分录和回执。比较保留 PostgreSQL 输出的精确 JSON 文本，不把金额先解码成浮点数；结构表及财务表必须非空，避免空备份假通过。它还验证源库没有变化、两边原会话均可解析，以及第二次恢复拒绝非空库。测试库保留到 CI 服务结束；运行后不要把该测试源库用于其他要求“未初始化管理员”的检查。

恢复校验从服务重读两个独立主体、分类模板副本、版本与归档状态、多资产账户和原币余额。当前夹具包括：

| 虚构场景 | 恢复后预期 |
| --- | --- |
| 个人期初、拆分支出与收入 | 75 USD / 90 EUR；原付款账户已归档，历史与成功回执仍可读取和重放 |
| 公司银行转现金、信用卡消费与还款 | 银行 700 USD、现金 200 USD、信用卡 0 USD；费用仅 100 USD |
| 明确资产精度 | ETH `1.000000000000000001`、指定虚构链的六位 USDC `12.345678`、零位 JPY `123` |
| 独立换汇账户 1000 USD，100 USD→90 EUR，另付 2 USD | 898 USD / 90 EUR；本金与费用回执各自保留，换汇规则见 [ADR 0007](../architecture/decisions/0007-exchanges-and-explicit-fees.md) |
| 独立 BTC 账户 1 BTC，支付 0.1 BTC，另付 0.00001 BTC | `0.89999000` BTC；本金和网络费不合并为一个原始金额 |
| 独立修订账户支出 100→120 USD，ETH 费用从最小单位改为两个最小单位 | 当前有效版本为 2；USD 880、ETH `0.999999999999999999`；原创建与更正回执均保留 |
| 同修订账户 USD/EUR 换汇带 USDC 费用，更正后归档账户并取消 | 最终取消版本为 3，最近入账仍为版本 2；换汇本金及 USDC 费用均抵消，EUR 0、USDC `12.345678` |

重放原收支、转账、换汇和 BTC 费用命令后，要求原回执不变；GET/列表使用当前状态中的 `latest_posting` 核对正常入账。对于已更正或取消的记录，旧创建回执仍表示原版本，修改命令也各自返回当时的状态回执；随后重读必须保持当前最终状态，不能被重放恢复成旧版本。取消记录须出现在明确的取消筛选结果中，并拒绝新的修改命令。规则见 [ADR 0008](../architecture/decisions/0008-operation-revisions-and-cancellation.md)。

审计校验要求版本连续，冲销与被冲销凭证的日期、描述和全部本金/费用行精确反向对应。对已封存的正常及反向凭证尝试追加完整平衡行或单独费用组成部分，必须命中封存约束；探测无论成功拒绝或异常放行都回滚，不能为了验证而留下写入。所有重放和拒绝探测后再次比较全部表，要求源库和恢复库均未改变。旧无费用回执不能凭空新增 `fees` 字段。

本地单测仅验证控制流程；恢复 checker 不能替代后续业务、异地灾备或生产服务器的恢复核对。实际命令、结果和 CI 证据保存在对应 PR 中。

变更日志与 identity 序列状态同样进入数据库和原件 bundle；恢复后核对日志、已保存游标与新写入序号的连续有效性。游标只代表其所属恢复快照，设备已经读取较新数据而服务器回到较早备份时的协调仍需完整同步协议。规则见 [ADR 0015](../architecture/decisions/0015-ordered-change-log.md)。

变更日志迁移须在暂停业务写入的维护窗口执行；已有日志时拒绝降级，不能以清空日志丢弃已发布游标。降级与业务写服务都先获取相同咨询锁再取其他业务/表锁，只读查询不获取写锁。

PostgreSQL 工具语义依据：[pg_dump 17](https://www.postgresql.org/docs/17/app-pgdump.html)、[pg_restore 17](https://www.postgresql.org/docs/17/app-pgrestore.html)。
