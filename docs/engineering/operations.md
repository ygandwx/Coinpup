# 基础数据库备份与恢复

本增量为 T01 提供手动 PostgreSQL 备份、隔离恢复和 CI 演练入口。当前可验证的数据是管理员、会话、登录节流状态及 Alembic 版本；后续业务表可进入整库 dump，但仍需增加各业务恢复验收。附件存储尚未实现，**此命令只备份数据库，不包含票据、公司证件、Logo、配置、密钥、PostgreSQL 角色或整个服务器**。完整附件恢复、自动定时、异地副本、保留策略和生产演练属于 T10。

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
3. 运行恢复；成功后在隔离环境核对管理员、登录会话、迁移版本及当前数据。切换应用连接是独立人工操作，此脚本不执行切换、不移除旧库。

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
python -m pytest tests/unit/test_backup_restore.py
```

这些检查覆盖现有备份不得覆盖、损坏备份拒绝、非空/错误/有其他连接的目标拒绝、工具失败传播、凭据不进入参数与环境，以及 POSIX 私有文件权限。Windows 单测跳过实际 POSIX 权限检查，不能把跳过写成已验收。

真实 CI 演练必须使用全新、一次性的 PostgreSQL 17 服务，先安装项目（例如 `pip install -e .`）并迁移源库。设置：

| 环境变量 | 内容 |
| --- | --- |
| `COINPUP_RUN_BACKUP_TESTS` | 必须为 `1`，显式允许建立测试管理员和新库 |
| `COINPUP_BACKUP_TEST_SOURCE_URL` | 已迁移、无管理员的专用测试源库 URL |
| `COINPUP_BACKUP_TEST_TARGET_URL` | 同一测试服务器、同一用户，库名固定 `coinpup_restore_test` 的 URL |

```sh
python scripts/check_backup_restore.py
```

checker 拒绝已存在的目标库和已有管理员的源库；只执行创建新库，不清空已有库。它创建随机测试密码及真实会话、备份、恢复，比较管理员/会话/节流/迁移表全部行，验证源库没有变化及两边的原会话均可解析，并验证第二次恢复拒绝非空库。测试库保留到 CI 服务结束；运行后不要把该测试源库用于其他要求“未初始化管理员”的检查。

本地单测通过仅验证控制流程。Linux CI 通过真实服务创建虚构主体、账本、多资产账户、分类、归档、期初及拆分收支，比较全部应用表的精确文本，并从恢复库重读结构与 USD/EUR/ETH/USDC/JPY 余额。随后重放原幂等请求，要求回执相同且数据库不变；尝试给已封存分录补行必须被拒绝。只有真实演练成功才记录通过；这仍不代表附件、后续结算、异地灾备或生产服务器已完成恢复验收。

PostgreSQL 工具语义依据：[pg_dump 17](https://www.postgresql.org/docs/17/app-pgdump.html)、[pg_restore 17](https://www.postgresql.org/docs/17/app-pgrestore.html)。
