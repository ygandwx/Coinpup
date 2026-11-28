# 应用运行与数据库/私有文件恢复

## 会话与部署配置

管理员由服务器 CLI 初始化或重置；容器密码重置命令为 `docker compose exec api python -m coinpup_api.admin reset-password`，交互输入新密码并撤销全部旧会话。初始化密码长度为 12–128 字符，不提供默认凭据。设计依据见 [ADR 0002](../architecture/decisions/0002-administrator-session.md)。

`COINPUP_ALLOWED_ORIGINS` 是 JSON 格式的精确 scheme/host/port 列表，开发来源示例见 [.env.example](../../.env.example)。生产模式须显式设置 HTTPS 来源，并核对反向代理、TLS 与 Cookie 的 Secure 行为。默认登录限制为 15 分钟内 5 次失败后限制 15 分钟，会话默认 12 小时；可配置项以 [Settings](../../services/api/src/coinpup_api/config.py) 为准。来源、CSRF 和会话不是可互相替代的检查，见 [API 约定](../architecture/api-conventions.md)。

## 账户加密密钥与 0118 升级

共用同一数据库的两个后端和一次性迁移进程必须取得**同一文件内容**。默认开发初始化会在宿主用户配置目录创建一把专用开发密钥，只在本地 `.env` 记录文件路径。正式环境在独立密钥管理器或受控离线主机生成；两台后端各从同一受控版本获取。文件只读挂载到 `/run/secrets/coinpup_account_key`，API 进程读取 `COINPUP_PRIVATE_KEY_FILE`；Compose 的宿主同名变量是源文件路径。文件授权给实际运行 UID（镜像内 10001，rootless 映射后核对）。限制宿主父目录遍历权限。迁移结束后，备份工具与 OCR 任务日常运行无需读取账户密钥。字段清单见 [ADR 0064](../architecture/decisions/0064-private-account-fields.md)。

首次初始化新密钥时，在仅托管人员可访问的目录运行下面的命令。路径与 ID 可记录到资产登记中。命令独占创建文件，终端不输出密钥。已加密数据库从托管处领取原文件。仅更换文件或 ID 会使新进程启动失败；本版本使用单密钥，轮换需要单独的重新加密迁移。丢失全部密钥副本将无法恢复私密数据。

```sh
python scripts/init_private_key.py "$protected_directory/account.key" "$key_id"
```

将密钥另行备份到有恢复授权和访问审计的保管位置。密钥保留时间覆盖所有对应 bundle。仓库、镜像、原件卷、普通 bundle、CI 制品和工单只允许出现非秘密的 ID 或路径。对代理、SQL 和应用日志关闭请求正文采集，以及调试时的请求/响应转储。

1. **准备期间保持现用入口和写入服务。** 构建并固定同一提交的两套候选后端和网页，准备迁移及密钥挂载。在隔离网络、独立 PostgreSQL 和原件卷，用虚构数据分别演练 `0113 → head`、含私密数据的 `0117 → head`，确认恢复后的授权查看和新建账户流程。验收完成前沿用原实例。
2. **确定切换边界。** 本迁移会独占 `accounts`、`account_assets`，并重写这两表及索引。按副本实测迁移耗时和峰值空间预留容量。正式切换需要能暂缓接受新写入并排空两套旧后端、设备推送和后台写任务的窗口。若业务要求全程持续受理写入，应继续服务旧版本，待有已验证的排队、追平和单写者切换机制后再安排。直接切换到先前恢复的副本会丢失备份点之后的写入。[PostgreSQL ALTER TABLE](https://www.postgresql.org/docs/17/sql-altertable.html)
3. **在获准窗口迁移最新状态。** 排空后记录最后变更游标、应用版本与镜像 ID，生成数据库和原件的一致 bundle，确认完成标记。保留原件和升级前 bundle，核对旧后端已停止接入。用候选镜像在唯一写入目标显式执行 `python -m alembic upgrade head`，核对 `alembic current` 为 `20261128_0118` 并执行 `alembic check`。分别核对两套后端的镜像 ID、启动密钥校验、就绪检查与已授权的只读业务探针，再将流量切回原有入口。旧版的就绪检查仅证明数据库连通。对未知提交结果重放原命令与原键。
4. **回滚按写入分界处理。** 迁移事务失败时核对当前 revision，再恢复与该 revision 匹配的旧服务。已提交迁移而尚无新写入时，可用维护窗口最后的 bundle 在空数据库和新原件卷恢复，核对最后游标和原件后成对切回旧镜像。已有新写入则保留当前数据库和原件，以兼容 `0118` 的构建修复；如需恢复，须先完整核对并追平新写入。两个版本各自只连接其兼容的库。
5. **恢复验收。** 用现有 `backup_bundle.py` 和 `restore_bundle.py` 恢复到新空库与新原件目录。恢复程序可在没有密钥时还原密文。启动恢复应用前从独立保管位置取得匹配的密钥。验证两套后端就绪、授权查看、移动摘要和鉴权下载原件。对测试副本另验证缺失密钥或同 ID 错密钥会阻止启动。先前的备份、WAL、旧副本和存储快照可能含旧明文，应按原访问限制保管；本迁移只改写当前库。

## 测试服务器部署

使用获授权的 `rootless` Docker context、Docker Engine 28 或更新版本及 Compose v2。将测试实例放在独立的 `main` 检出目录和 `coinpup-mvp` Compose 项目中。Git 检出使用常规文件权限，私有环境文件单独限制为仅部署账户可读。只使用明确虚构的数据。邮件保持关闭，测试邮件只投递到隔离的本地捕获器。参见 [Docker 回环端口发布](https://docs.docker.com/engine/network/port-publishing/)。

在检出目录按 [README 的 Docker Compose 步骤](../../README.md#docker-compose)执行。`docker info` 的安全选项应包含 `rootless`，服务器版本应满足上述条件。管理员密码通过 CLI 的隐藏提示输入。

```sh
docker --context rootless version --format '{{.Server.Version}}'
docker --context rootless info --format '{{json .SecurityOptions}}'
(umask 077; python scripts/init_env.py)
docker --context rootless compose -p coinpup-mvp build api
docker --context rootless compose -p coinpup-mvp up -d --wait postgres
docker --context rootless compose -p coinpup-mvp run --rm --no-deps api python -m alembic upgrade head
docker --context rootless compose -p coinpup-mvp up -d --wait api
docker --context rootless compose -p coinpup-mvp exec api python -m coinpup_api.admin create --username admin
docker --context rootless compose -p coinpup-mvp ps
docker --context rootless compose -p coinpup-mvp port api 8000
docker --context rootless compose -p coinpup-mvp port postgres 5432
```

为定时备份保留源服务。首次启动或重新创建项目容器后，按项目容器设置重启策略。

```sh
api_id=$(docker --context rootless compose -p coinpup-mvp ps -q api)
pg_id=$(docker --context rootless compose -p coinpup-mvp ps -q postgres)
test -n "$api_id" && test -n "$pg_id" &&
  docker --context rootless update --restart unless-stopped "$api_id" "$pg_id"
```

确认网页和 PostgreSQL 的两个发布端口仅绑定宿主机 IPv4 回环，客户端只通过现有 SSH 入口转发网页端口。替换命令中的占位符，打开 [README 所示的回环网页](../../README.md#docker-compose)，确认服务就绪并验证登录。沿用网页端口以匹配 Compose 允许的浏览器来源。

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -L '<客户端回环地址>:8000:<服务器回环地址>:8000' '<已有 SSH 登录目标>'
```

### 每日数据库与原件备份

先在 `rootless` context 创建独立专用卷，在容器内为 API 运行用户建立私有目录。

```sh
docker --context rootless volume create --label com.coinpup.test-project=coinpup-mvp coinpup-mvp-backups
docker --context rootless volume create --label com.coinpup.test-project=coinpup-mvp coinpup-mvp-recovery
docker --context rootless compose -p coinpup-mvp run --rm --no-deps --user 0:0 \
  -v coinpup-mvp-backups:/backups -v coinpup-mvp-recovery:/recovery \
  api install -d -m 0700 -o coinpup -g coinpup /backups/bundles /recovery/files
```

在独立检出目录把由 `scripts/init_env.py` 生成的 `.env` 保持为 `0600`。备份程序从该文件读取生成的测试数据库密码，显式指定本项目源库。将以下程序保存到 `~/.local/bin/coinpup-mvp-backup` 并设为仅部署用户可执行。

```bash
#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ -f .env && ! -L .env && -O .env && $(stat -c '%a' .env) == 600 ]]
mapfile -t passwords < <(awk '/^POSTGRES_PASSWORD=/{print substr($0,19)}' .env)
[[ ${#passwords[@]} == 1 && ${passwords[0]} =~ ^[0-9a-f]{48}$ ]]
export COINPUP_BACKUP_DATABASE_URL="postgresql+psycopg://coinpup:${passwords[0]}@postgres:5432/coinpup"
exec docker --context rootless compose -p coinpup-mvp run -T --rm --no-deps \
  -v coinpup-mvp-backups:/backups -e COINPUP_BACKUP_DATABASE_URL \
  api python scripts/backup_bundle.py --storage-root /app/data/files \
  --output "/backups/bundles/$(date -u +bundle-%Y%m%dT%H%M%SZ)-$$"
```

将以下单元保存在部署用户的 systemd 用户配置目录。将 `WorkingDirectory` 改为独立检出目录。定时器依赖服务器已提供的持久用户服务环境。

`coinpup-mvp-backup.service`

```ini
[Unit]
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=%h/coinpup-mvp
UMask=0077
ExecStart=%h/.local/bin/coinpup-mvp-backup
```

`coinpup-mvp-backup.timer`

```ini
[Timer]
OnCalendar=*-*-* 03:30:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

先使用测试管理员添加一份虚构原件，再启动定时器和一次手动备份。按 [bundle 说明](#包含票据与证件的-bundle)核对 dump、私有原件与最终发布的 `manifest.json`，保留数据卷和备份卷。

```sh
systemctl --user daemon-reload
systemctl --user enable --now coinpup-mvp-backup.timer
systemctl --user list-timers coinpup-mvp-backup.timer
systemctl --user start coinpup-mvp-backup.service
journalctl --user -u coinpup-mvp-backup.service -n 20
```

### 手动空目标演练

将已完成且包含虚构原件的 bundle 目录名设为 `COINPUP_MVP_BUNDLE`。以 Bash 运行下列命令创建新的隔离空库，然后通过隐藏提示输入只指向该空库的恢复 URL。测试应用继续连接原测试库，恢复命令在独立目录创建原件。

```bash
(
  set -e
  bundle_dir=${COINPUP_MVP_BUNDLE:?Set the completed bundle directory name}
  restore_db="coinpup_restore_$(date -u +%Y%m%d_%H%M%S)"
  docker --context rootless compose -p coinpup-mvp exec -T postgres \
    createdb --username coinpup --template template0 "$restore_db"
  read -r -s -p 'Empty recovery database URL: ' COINPUP_RESTORE_DATABASE_URL
  printf '\n'
  export COINPUP_RESTORE_DATABASE_URL
  exec docker --context rootless compose -p coinpup-mvp run --rm --no-deps \
    -v coinpup-mvp-backups:/backups:ro -v coinpup-mvp-recovery:/recovery \
    -e COINPUP_RESTORE_DATABASE_URL api python scripts/restore_bundle.py \
    --backup "/backups/bundles/$bundle_dir" --storage-root "/recovery/files/$restore_db" \
    --confirm-empty-database "$restore_db"
)
```

对照同次备份核对迁移版本、虚构主体、账务金额和原件 SHA-256。保留源实例及演练证据，实测结果记入相应 PR。

### 测试实例升级与回滚

1. 确认目标为仅使用虚构数据的测试项目。保存运行中的 Git 提交、API 镜像 ID 和对应的私有配置。暂停升级目标的写入和定时任务，从旧版本先生成一份数据库与原件 bundle，等待命令成功并确认 `manifest.json` 已发布。保留该 bundle、旧镜像和旧检出目录。对正在使用的测试实例做演练时，从 bundle 恢复出隔离副本，源实例仍可使用。
2. 取得 `origin/main` 的准确提交，确认该提交通过 CI，在独立检出目录构建候选 API 镜像。将镜像及提交对应起来，保留候选的确切镜像 ID。按[手动空目标演练](#手动空目标演练)准备独立数据库和新的原件目录。演练的迁移和应用均使用该副本自己的 `COINPUP_DATABASE_URL`，连接前核对目标是本次隔离库。

```sh
git fetch origin main
new_commit=$(git rev-parse FETCH_HEAD)
git worktree add --detach "$upgrade_checkout" "$new_commit"
```

3. 在同一测试网络中用候选镜像执行 `python -m alembic upgrade head`，记录前后的 `python -m alembic current`。然后用候选镜像启动应用至单独的回环端口，用 `/api/v1/health/ready` 验证就绪。登录核对虚构主体、账务、版本和私有原件。记录运行 `upgrade head` 前后的实际版本，即使版本保持一致也记录。

```sh
docker --context rootless run --rm --network "$test_network" \
  -e COINPUP_DATABASE_URL "$new_image" python -m alembic upgrade head
```

4. 回滚演练另外新建一个 `template0` 空库与一个从未使用的原件目录，恢复步骤 1 已验证的 bundle。把备份卷只读挂载，用旧镜像执行 `restore_bundle.py`，传入与实际空库名一致的 `--confirm-empty-database`。随后只让旧镜像连接这套恢复目标，检查它的就绪端点、数据库版本和同一批虚构数据与原件。禁止对升级后的数据库运行 `alembic downgrade`，也禁止将旧镜像指向已升级的库。保留升级目标供检查，切换连接与清理副本另行处理。

以下命令使用先前记录的 `old_image`。通过私有环境将 `COINPUP_DATABASE_URL` 指向回滚空库的恢复结果，使 `COINPUP_FILES_DIRECTORY` 指向恢复原件目录，并将 `rollback_bind` 与 `COINPUP_ALLOWED_ORIGINS` 设置为对应的回环地址和端口。

```sh
docker --context rootless run -d --rm --name "$rollback_api" --network "$test_network" \
  --publish "${rollback_bind:?Set the selected loopback host and port}:8000" \
  --mount type=volume,src=coinpup-mvp-recovery,dst=/recovery,readonly \
  -e COINPUP_ENVIRONMENT=development -e COINPUP_DATABASE_URL \
  -e COINPUP_FILES_DIRECTORY -e COINPUP_ALLOWED_ORIGINS "$old_image"
```

为演练应用显式配置回环发布地址和对应的 `COINPUP_ALLOWED_ORIGINS`。源实例与两个演练目标使用各自的数据库和文件路径。恢复验证期间保持两目标的调度关闭。结束后停止临时应用，确认源测试实例仍就绪，将命令、版本、就绪与业务比对结果记录到对应 PR。

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

## 周期 Invoice 草稿维护任务

在已迁移的 API 容器中显式执行一次：

```bash
docker compose exec -T api python -m coinpup_api.jobs recurring-invoices --per-rule 10
```

任务使用当前 UTC 时刻换算每条规则保存的 IANA 时区；每条规则每次最多处理10个待生成周期，可配置1–100。一次调用先捕获有限规则列表，再按规则原起点补齐连续周期；新增规则留到下次。暂停规则或归档主体不生成，恢复从首个未生成日期继续，不自动跳过积压。源草稿编辑/归档不改已捕获模板，模板变化须显式刷新规则。

每个实例独立提交，沿用咨询锁→账本→主体→资产的锁序。重复或并发执行只确认已有实例，不覆盖人工修改的草稿；JSON中的confirmed是本次核对成功的实例数，可能包含并发重放，不能当作本次独占新建数量。单条规则失败保留其进度，其他规则继续；失败返回非零退出码和稳定错误码，不输出模板、金额或连接配置。提交后响应丢失时，下次从数据库已保存的连续进度继续。

此入口只生成草稿，不入账、不分配正式编号、不发送客户邮件。尚未自动部署定时器；T10再配置宿主机cron或简单Compose调度容器。升级/恢复期间停止调用，核对数据库与原件后恢复调度；已恢复实例不得重复生成。

### 提醒邮件测试捕获

测试实例使用与任务容器共享网络命名空间的本地 SMTP 捕获器。禁用捕获器的邮件转发和公网端口，仅使用虚构地址与事项。任务连接捕获器的回环端口，默认 1025，可用 `COINPUP_REMINDER_MAIL_CAPTURE_PORT` 选择测试端口。

启用源测试实例的调度时，为任务单独设置 `COINPUP_REMINDER_MAIL_CAPTURE_ENABLED=true`。以下命令只处理指定所有者及账本，输出扫描数和投递状态计数。

```bash
python -m coinpup_api.jobs reminder-email --owner "$owner_id" --ledger "$ledger_id"
```

双渠道任务支持按所有者扫描，也可以通过 `--ledger` 限制为一个账本。站内通知独立执行。设置测试 SMTP 捕获开关后，同次任务也处理启用的邮件，两个结果分别计数。

```bash
python -m coinpup_api.jobs reminder-delivery --owner "$owner_id"
```

用部署现有的定时器周期调用该单次任务。任务每次核对最新日期与渠道同意，处理已到计划时点的事项。原有成功投递和首个已读时间随数据库与原件 bundle 保留。将已交付实例复制为恢复目标时，沿用下面的调度暂停流程。

升级切换和恢复演练期间暂停提醒任务。恢复目标保持默认关闭。先核对恢复点后的 SMTP 接受记录，再决定恢复目标上的后续任务。邮件结果未知时保留原投递证据，待核实后标记收到或停止通知。生产 SMTP 配置需要单独授权。[ADR0038](../architecture/decisions/0038-reminder-delivery.md)

### 生产提醒邮件

在获授权的提醒任务环境配置 `COINPUP_ENVIRONMENT=production`，并显式启用 `COINPUP_REMINDER_MAIL_SMTP_ENABLED`。通过任务的私有部署配置提供 `COINPUP_REMINDER_MAIL_SMTP_HOST`、`COINPUP_REMINDER_MAIL_SMTP_PORT`、`COINPUP_REMINDER_MAIL_SMTP_MODE` 和 `COINPUP_REMINDER_MAIL_SMTP_SENDER`。模式为 `starttls` 或 `ssl`，使用系统信任库核对服务器证书和主机身份。需要认证的中继通过 `COINPUP_REMINDER_MAIL_SMTP_USERNAME` 与 `COINPUP_REMINDER_MAIL_SMTP_PASSWORD` 成对配置。允许免认证的中继可同时省略两项。

生产传输默认关闭。启用后，`reminder-email` 和 `reminder-delivery` 使用上述传输。配置无效时，双渠道任务仍执行站内通知，邮件结果为空，并返回非零退出码。SMTP 已接受或发送结果未知的状态继续遵循原重放边界。恢复目标保持生产传输关闭。[ADR0038](../architecture/decisions/0038-reminder-delivery.md)

## 公共报价更新

读取报表和移动端缓存使用数据库中的已保存报价。外部更新默认关闭。为已获许可的服务器出站流量启用单次任务时，显式设置 `COINPUP_MARKET_QUOTES_ENABLED=true`，并传入所有者与一个直接币对。只向提供方发送公开的币对和参考日期。错误只输出通用原因，已有缓存继续可以读取。

```sh
docker compose exec -e COINPUP_MARKET_QUOTES_ENABLED=true api \
  python -m coinpup_api.jobs market-quotes \
  --owner '<所有者 UUID>' --asset EUR --display USD
```

法币使用 Frankfurter 中固定到 ECB 的参考数据，支持可用的直接历史报价日。要查询某日，可加 `--price-date YYYY-MM-DD`。只有参考日时，具体市场时点为空，读取仍显示获取时间。BTC 或 ETH 对 USD、EUR、GBP 使用 Coinbase Exchange 当前成交报价。可定期保存所需币对以积累历史缓存。未知币对或日期使用缺价状态，也可由用户同日手动补录。来源与覆盖说明见 [Frankfurter 提供方文档](https://frankfurter.dev/providers/ecb/) 和 [Coinbase Exchange 成交报价文档](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-ticker)。

现有任务调度器可定期执行同一命令。重复获取相同观察会复用其 ID 和首次获取时间。测试使用离线的虚构回复，开启任何涉及收费、账号凭据或新增网络权限的服务前按照项目授权流程处理。

## 备份范围

余额与交易日/归属日的查询索引通过显式 Alembic 迁移建立，模型定义见 [账务模型](../../services/api/src/coinpup_api/ledger/models.py)。普通建索引在迁移事务中执行，安排维护窗口；降级只删除本次新增索引，不改财务行或回执。测试中的 `enable_seqscan=off` 仅验证索引适用性，不是部署设置，也不代表实际性能提升。

财务回执的 `hash_version` 随数据库一起备份。升级到摘要 v2 后，存在任何 v2 回执就拒绝降级到无版本字段的结构；不得改写或删除回执来规避保护。旧 v1 回执升级后保持原响应与重放规则，见 [ADR 0014](../architecture/decisions/0014-versioned-command-hashes.md)。

**`backup_database.py` 始终只备份数据库；有原件时使用 `backup_bundle.py`。** 两者都不包含配置、密钥、PostgreSQL 角色或整个服务器；自动定时、异地副本、保留策略与生产演练属于 T10。

## 卡号与备份保管

信用卡和借记卡的完整卡号保存在 PostgreSQL 中，数据库备份与 bundle 都包含这些私密资料。限制数据库文件、备份、恢复目标和副本的读取权限，使用私有存储与加密传输。公开或分享账本资料时使用掩码。数据库和反向代理日志保持隐藏绑定参数和请求正文。网页通过会话鉴权和 CSRF 校验请求临时显示卡号，移动端同步仅使用末四位及有效期。已有卡号或借记卡记录的版本回滚使用独立空目标恢复。

## 加密账户资料与回滚

账户平台的 UID、邮箱、备注和逐币资料随 `accounts`、`account_assets` 进入私有数据库备份和 bundle。恢复时核对平台、网络、地址和币种对应关系。保存过这些资料的数据库即使已清空字段也会拒绝降级。将升级前的 bundle 恢复到独立空目标后启动旧版本来回滚。

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

CI 的 `check_compose_backup.py` 先验证旧数据库单独恢复，再执行 `check_bundle_restore.py`：两个账本同内容独立、同账本去重、PDF/PNG、归档文件/关联、取消流水、未完成上传、完整表与原件字节一致、恢复后原回执重放。演练在导出快照后、pg_dump 前提交一次新上传，确认晚到记录与 staging/孤立 blob 都不进入备份。 新增虚构100 USD已结账单/付款，核对同账本及跨账本银行原件只增加证据，经营费用100、银行扣款100、应付零不变；归档/结账后恢复，原OCR付款关联、付款v2回执与原件上传逐字重放，所有表和文件均不新增。

经营结算夹具另覆盖真实收付款、独立手续费、同币/跨币初始与后续部分分摊和未分配预付款。恢复后核对资金及各维度往来余额，在原账本结账、往来/资金账户/主体归档后重放原始v2命令，要求原回执和全部表不变；不是生产数据迁移或切换。

分摊退回夹具补充同币/跨币的部分、末次退回及释放后重新分摊。完整bundle在隔离空目标恢复后，比对原付款/原分摊/退回回执、原来源容量、新分摊和净余额；保留结账归档状态重放原JSON，全部表（含changes）必须不变。它复用现有真实数据库和私有原件恢复检查，不运行OCR验收或处理真实账单。

退款夹具覆盖客户/供应商、同币/跨币、显式退回后分次退款及未分配预付款全额退款；本金不改变经营分类，独立手续费只计一次。新事实保存后再次结账并归档资金账户/主体，隔离恢复后核对原付款、分摊、退回、退款回执及耗尽本金容量，重放原JSON不改变任何表或changes。

贷项修订夹具另用独立虚构账本，保存两种单据的完整成员更正和取消；结账/归档后核对首版201、全部历史200、当前净税容量及稳定组通知。修订中新建而后取消的成员关联一份私有PDF，随空目标bundle恢复并按原上传ID重放；全部表、changes和原件字节不变。取消组不恢复旧成员，旧普通财务入口仍隔离新增及历史贷项子操作；保留既有主体归档拒绝的优先级。不改变此前退款、付款和原贷项的恢复证据，也不运行OCR质量验收。

同一静止夹具另核对当前组、版本目录和指定修订详情的API读取服务完整返回；各服务自行鉴权并取得只读快照，不宣称多次调用共享一个事务或替代HTTP浏览器验收。原创建详情及原JSON/幂等键重放仍独立验证，不能用读取成功代替原意图恢复。

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

CI 的 PostgreSQL 17、Node 24 和 Python 3.12 基础镜像使用固定摘要的 [Docker 官方 ECR Public 渠道](https://www.docker.com/blog/news-from-aws-reinvent-docker-official-images-on-amazon-ecr-public/)，避免共享 runner 的 Docker Hub 匿名拉取限流，无需新增凭据。浏览器 Compose 覆盖启动前运行 `python scripts/prepare_browser_ci_image.py`，生成仅替换 web/api 两个 FROM 的 `build/Dockerfile.browser-ci`；原 Dockerfile、OCR stage 和本地默认 Compose 不变，预期基础镜像发生变化则准备步骤直接失败。

升级时核对 Docker Hub 与 ECR 对应镜像清单摘要一致，同步更新 CI/浏览器覆盖的 PostgreSQL 引用和准备脚本的 Node/Python 映射，并通过迁移、浏览器及完整恢复检查。拉取失败仍使检查失败，不跳过测试。

浏览器 CI 用 `prepare_browser_ci_image.py --pull` 预取这三个固定镜像：优先官方 ECR，失败或120秒超时后仅回退一次至 Docker Hub 官方同名、同摘要镜像；两处均失败即停止。成功后生成仅含 PostgreSQL 镜像选择的 `build/compose.browser-images.yaml`，追加至该任务的 `COMPOSE_FILE`，并将所选 Node/Python 引用写入生成 Dockerfile。后续迁移及恢复复用同一覆盖；本地默认不预取，不更改部署/OCR镜像，不需要登录或凭据。

PR 代码变更运行快速层，包含数据库集成测试和浏览器冒烟集。规模压力用例按 `scripts/db_ci_pytest.py` 的规则归入全量层。main、夜间和手动运行执行完整数据库与浏览器套件。纯文档 PR 保留必需检查的报告，数据库分区、发票数据库验证和浏览器容器无需执行。识别文档变更时同时考虑移动文件原路径的删除和新路径的添加。

数据库 CI 使用八个独立的 Ubuntu runner。各自用 `python -m scripts.start_database_ci` 按固定摘要及双源拉取规则启动 PostgreSQL 17，健康检查成功后运行 `python scripts/check.py db`。每个分区执行升级、空库降级、再次升级及模型一致性检查，然后运行本层分配的集成测试。固定映射及其余模块的分配见 `scripts/db_ci_manifest.py`。每个分区限时 40 分钟，失败时其他分区继续。运行步骤未成功（含取消）时保留 stderr、资源和数据库诊断，再清理容器。外层强制终止可能使原始退出码或清单缺失，仍需查明原因。容器仅绑定 runner 回环，使用虚构凭据，结束后清理容器和临时卷。镜像拉取或就绪失败会使该分区失败。

单个用例运行满 60 秒时，数据库插件在持有 GIL 的 Python 线程中将用例名和各线程栈写到标准错误，并继续等待原用例完成。子进程因信号结束时，统一检查入口记录原始负返回值和信号，返回对应的 shell 状态码。

各分区分别上传本次提交的测试清单与执行结果，保存三天。重跑单个分区会替换该次运行的同名报告。`PostgreSQL migrations and integration tests` 在所有八个分区成功后核对同次运行、同一 SHA、同一层的报告，并验证该层每个用例恰好执行一次且通过。启用数据库检查时，任一分区失败、跳过、取消或报告缺失都会使汇总失败。容器任务独立验证真实浏览器与空目标备份恢复。

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

退款修订恢复演练在独立虚构账本建立多版更正和终态取消，将退款操作关联的私有 PDF 与数据库放入同一 bundle。空目标恢复后读取当前事实与逐版历史，重放原创建、退回及每版修订的冻结请求，核对付款本金、现金、控制余额、独立手续费、证据顺序、文件内容与关联。

分摊退回修订演练建立跨资产的换源、重新加入和终态取消，核对重新加入的永久序位与新操作。有效退款先引用退回 v2，显式更正至另一有效组后才可取消原组；恢复后核对两个证据版本、已移除成员的私有 PDF、目录、逐版详情与原始 JSON 幂等重放。

## Invoice 字体准备

T06-6 的离线字体构建使用已锁定的 fonttools 4.66.1；在独立构建环境安装
`requirements-benchmark.lock` 中的锁定依赖，不运行 OCR 或识别质量验收。
输入为 `invoice_fonts.py` 固定版本的 Noto SC/TC 原始字体及原 LICENSE；
其来源版本与既有 `tests/fixtures/ocr/font-assets.json` 一致。构建器不联网。

在仓库根目录、Python 包可导入的环境运行
`python scripts/build_invoice_fonts.py FONT_SOURCE_DIR NEW_FONT_OUTPUT_DIR`。
输出目录必须不存在；两个完整字库按固定权重生成并改名，保留授权信息，
不按测试语料删减字形。构建结果必须匹配应用固定的字节大小与 SHA-256。
运行时读取同一目录内的两份字体及 LICENSE，拒绝缺失、篡改、链接和替代系统字体。
字体输出供后续受限 PDF 渲染使用；此命令不分配 Invoice 号码、生成正式文件或部署服务。

## Invoice 渲染依赖

`invoice` 可选组单独锁定在 `requirements-invoice.lock`，沿用已审阅构建锁中的
ReportLab 5.0.1、Pillow 12.3.0 和 charset-normalizer 3.5.2；现有 API 锁与 OCR 锁不变。
在独立渲染环境运行 `python -m pip install --require-hashes -r requirements-invoice.lock`，
然后运行 `python scripts/check_invoice_dependencies.py` 核对实际平台的 wheel 版本与通知。
所有包内授权文件随制品保留，包括 ReportLab 附带字体的声明；Invoice 只使用上述固定字库。
该运行环境不安装识别引擎、fontTools 或 PDF 验证工具。Linux API 镜像按哈希安装此锁；普通源码开发环境仍可仅安装基础锁。
依赖安装及审计不等于渲染服务部署、正式 Invoice 发行或 OCR 验收。

构建 API 镜像时独立字体构建阶段获取已固定的源文件和许可证，重新核对大小、摘要及生成品；运行阶段仅复制两份完整静态字体和 LICENSE 到只读安装目录 `/opt/coinpup/invoice-fonts`。默认 `COINPUP_INVOICE_FONT_DIR` 指向该目录；运行账号在构建时实际读取并核验两份字库和许可证，发行时不联网、不使用系统字体。无法获得固定构建输入时明确停止构建，不替换素材。该镜像准备不等于生产部署。

## Invoice 文本渲染检查

`python scripts/check.py invoice` 显式运行依赖审计与真实 PDF 检查；缺少可选包或
`COINPUP_INVOICE_FONT_DIR` 指定的完整固定字库会失败。验证环境同时按哈希安装
`requirements-invoice.lock` 与 `requirements-benchmark.lock`，后者仅提供构建字体和
读取 PDF 的测试工具；普通 API 请求不调用渲染器。CI 在必需 Quality 任务中执行该入口，
不运行 OCR 正式验收或修改其门槛。

另用 `python scripts/check.py invoice-db` 验证真实渲染、提交及数据库/原件恢复。仅面向一次性本机 PostgreSQL 17：显式设置 `COINPUP_ENVIRONMENT=test`、`COINPUP_RUN_DB_TESTS=1`、`COINPUP_RUN_BACKUP_TESTS=1`，使用回环地址的 coinpup 测试库和同名测试角色；需要 17 版客户端、固定字体和上述验证锁。将仓库根、`scripts`、`services/api/src` 加到 PYTHONPATH。该检查会重写测试夹具，并创建独立空库和私有文件目录；不指向已有业务库。

文本核心仅消费已授权预留的冻结字节与保存的摘要，标签中英切换、原文直接绘制；金额
始终使用保存的十进制字符串，不重新计价。不支持的字形明确拒绝，不替换为空白方块。
内核限制 256 页及 8 MiB 输出；`render_invoice_pair` 在 Linux 固定子进程同时生成中英两份。
含 Logo 的快照必须另传其冻结身份对应的原件字节；缺失时明确拒绝，不能静默省略。
本阶段不写原件、分配号码或开放发行 API。

重生成锁文件：

```sh
python -m piptools compile pyproject.toml --extra invoice --constraint requirements-benchmark.lock --output-file requirements-invoice.lock --generate-hashes --strip-extras --no-emit-index-url --no-emit-trusted-host
```

## Invoice 受限渲染进程

调用方须先鉴权取得其预留的冻结字节、保存的摘要和受信任本地字体目录；摘要本身不是
访问授权。固定子进程在加载应用和可选包之前限制 CPU 30 秒、地址空间 1 GiB、文件
8 MiB、64 个描述符并禁用 core；已有更低软/硬限制保持更低。父进程限制总墙钟 45 秒
及两份 PDF 加固定帧头的合并输出；测试/运维预算只能调低，不能增大。

临时目录为 0700、快照为 0600；子进程不继承数据库凭据、Python 启动覆盖或代理环境。
两种语言均成功后才输出完整帧，父进程核对归属摘要、长度和两份内容哈希。超时、资源
超限、输出不完整或清理失败均不返回完成结果；清理整个进程组、回收直接子进程并删除
临时输入。此限制不是网络/文件系统安全沙箱，不执行用户模板或外部命令。

原冻结数据始终是失败后的恢复来源。本包不写原件、续租/提交发行或开放 API。
未来发行服务必须持有有效预留并重新核对归属后，才能原子关联两份原件。

## Invoice Logo 解码边界

只接收已鉴权的原件字节，不接收路径/URL；在固定子进程中核对冻结 Logo 的字节大小、
摘要及 PNG/JPEG/WebP 格式，再校验并完整解码。处理上限为编码 8 MiB、每边 4096 像素、
总计 4,000,000 像素；超限明确拒绝，不静默缩图。仅支持单帧及 1/L/LA/P/RGB/RGBA/CMYK
模式；动画、未支持位深、损坏或截断图片均拒绝。这是 Logo 渲染限制，未改变原件上传上限；
后续 API/网页应在预留前明确这些处理限制。

按 EXIF 方向显示，保留透明度与宽高比；PDF 使用解码后的 RGBA 像素，布局至最大
144×72 point 区域，忽略源 DPI，不转存 EXIF/ICC/XMP 等元数据。没有色彩管理或重新采样；
不修改原 Logo 字节和身份。输入以 0600 临时文件交给子进程，失败时沿用原快照恢复。

## Invoice 双语原件准备

`prepare_invoice_blobs` 只在已鉴权的冻结输入上工作，调用时不得持有数据库事务。
先由固定子进程完成两份 PDF，再通过既有 FileStore 分别暂存、核对摘要/大小/格式；
两份暂存齐备后才持久发布，并重新打开校验两份 blob。文件和目标目录 fsync 沿用原件
协议，输入文件名不参与路径。返回的 `PreparedInvoiceBlobs` 绑定快照摘要，但不是
文件元数据、发行回执或提交成功证明；调用方仍须在短事务中重验归属、快照和预留。

失败或取消不返回完整结果；等待拥有的渲染/文件线程结束，并屏蔽取消以尝试清理全部
暂存 token。FileStore 的清理沿用既有尽力执行约定，I/O 失败可能保留暂存现场；不会
删除任何已发布 blob 来回滚。部分发布、竞争或未知结果留下的孤立原件按 ADR0012 保留。
重试仍使用原冻结输入/意图，规范文件去重及元数据封存属于后续发行事务，本包不开放 API。
