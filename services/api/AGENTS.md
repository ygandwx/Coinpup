# 后端、账务与 Python 开发规则

修改 `services/api/**`、`tests/**` 或 Python 脚本前阅读本文件，并遵守[根开发规则](../../AGENTS.md)。接口通用合同见 [API 约定](../../docs/architecture/api-conventions.md)，按任务读取 [ADR 索引](../../docs/architecture/decisions/README.md)和对应[业务验收](../../docs/product/acceptance.md)。

## 金额与写入边界

- 金额只能经由 `Amount` 和完整 `AssetDefinition` 处理；外部入口只接受十进制字符串，资产身份包含精度、网络及适用的代币标识。
- 金额计算禁止 float，禁止依赖 `Decimal` 默认上下文。使用整数最小单位进行计算，构造数据库 Decimal 必须来自精确字符串。
- 分录写库前必须经过 `prepare_journal_lines`，校验实际资产、引用、精度、范围、行形状及逐资产配平；数据库类型保持 `NUMERIC(38,18)`。
- 已提交的分录和行封存，不能更新、删除或补行。更正追加完整冲销及替代分录，取消只追加冲销；保留连续版本链、全部旧回执和同账本复合外键隔离。

## 新增财务命令的顺序

1. 定义请求与回执 schema，用 `kind` 判别联合明确业务类型。
2. 实现服务和完整原子事务；获取锁的顺序为账本 → 主体 → 排序后的资产，不能改变该顺序或漏掉涉及的资产。
3. 验证分录形状与逐资产配平，检查本金、所有手续费以及最终账户资产余额。
4. 增加相应数据库约束，并保留归属、不可变和封存保护。
5. 明确幂等摘要与永久回执，保留原金额字符串和旧请求/回执兼容；OPT-20 采用后才按其新规则实施，不能提前变更摘要。
6. 更新代码生成的 OpenAPI 快照，不手工改字段契约或快照。
7. 完成下面的测试矩阵，验证事务真正提交后的结果及失败路径。

## 金额、分录与幂等测试矩阵

涉及金额的改动必须覆盖：

- 每种资产的余额守恒，不同资产不互相抵销。
- 费用不重复计算。
- 精度边界：最小单位、最大值、拒绝超精度输入。
- 同键重放返回原回执，同键不同请求返回 409。
- 并发的同键请求只入账一次。
- 任何一步失败都整笔回滚。
- 账本之间互相隔离。
- 归档或停用之后，已有回执仍可重放。
- 备份恢复后重放，任何表都不发生变化。

在仓库根目录运行 `python scripts/check.py`；数据库检查只对显式指定的一次性测试库运行 `python scripts/check.py db`。测试数据必须明确虚构，不使用真实账单、公司资料或密钥。相关恢复检查保留数据库与原件 bundle 的空目标验证；实际命令、结果和环境限制记录在 PR。

## 迁移与错误

- Alembic 迁移显式执行，使用冻结的 SQL，不 import 应用代码。存在受保护历史时必须拒绝降级，不能删历史或削弱触发器来通过降级。
- 新增索引或约束同时写入 SQLAlchemy 模型，保持 `alembic check` 一致；合并前 `python -m alembic heads` 只能有一个 head。
- 领域错误返回稳定的 `code` 和通用消息，保留现有错误合同；不回显输入，不泄露 SQL、连接信息、密钥或服务器路径。

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

