# ADR 0002：单管理员初始化与可撤销会话

- 日期：2026-10-03。
- 状态：T01 实施；实际验证与集成状态见 [项目进度](../../engineering/status.md)。
- 承接：[ADR 0001](0001-modular-api-foundation.md)。

## 问题与决定

首版由一位使用者管理私人财务，需要先建立访问边界。管理员只能通过具备服务器访问权的操作员运行初始化命令创建，不提供公开注册或未认证的 HTTP 初始化接口。初始化以数据库约束和事务防止并发创建多位管理员。

密码使用 Argon2id 散列，不存明文。命令行通过隐藏输入获取密码，不能把密码放入命令参数或普通日志。忘记密码时在服务器运行重置命令；重置撤销已有会话。

网页使用随机、不透明的会话令牌，由 HttpOnly / SameSite=Strict Cookie 携带；生产模式启用 Secure，配置的浏览器来源必须为 HTTPS。数据库只保存会话令牌摘要及过期信息，退出时服务端撤销，重启服务后未过期会话可继续验证。

登录及有副作用的会话操作验证允许的 Origin；已认证写操作还需 CSRF token。CSRF token 只保存在网页内存，刷新后从会话接口取得。SameSite 作为额外保护，不能替代上述检查。后续账务写接口必须复用此访问检查，不自行放开跨域或跳过 CSRF。

失败登录计数和临时限制保存在数据库，避免进程重启或多 worker 绕过。错误响应保持通用，不返回原始数据库异常或密码输入。

## 接口与运行

- `POST /api/v1/auth/login`：验证凭据并建立新会话。
- `GET /api/v1/auth/session`：获取当前管理员和 CSRF token，无会话返回 401。
- `POST /api/v1/auth/logout`：校验 CSRF 后撤销当前会话并清除 Cookie。
- 初始化、密码重置通过 `python -m coinpup_api.admin` 命令完成。

开发时 Vite 转发 `/api`；容器直接由 FastAPI 提供构建后的网页，两种方式都保持浏览器同源请求。生产 HTTPS 终止与域名配置属于实际部署验收；开发 Compose 只绑定本机，不能作为已完成生产部署的证据。

## 影响与备选方案

数据库会话便于立即撤销和审计，代价是每次认证读取数据库。本阶段没有选择仅靠 JWT 自包含鉴权，避免引入撤销列表及刷新令牌轮换的额外复杂度。后续原生 App 身份流程在 T11/T12 单独确定，不把浏览器 Cookie 直接当作完整移动端协议。

备份包含管理员和会话的敏感数据，必须限制访问。恢复到新环境后可以运行密码重置来撤销恢复出的旧会话。当前备份只覆盖已经存在的数据库；附件实现后必须扩展恢复验收，不能提前宣称覆盖票据。

## 验证要求

验证正确/错误密码、失败限制、过期/撤销会话、跨来源请求、错误 CSRF、并发初始化、密码重置撤销会话、响应脱敏；使用真实 PostgreSQL 迁移及浏览器登录/刷新/退出验证闭环。另在空目标库验证备份恢复，对非空目标和损坏备份应拒绝。

## 设计依据

- [OWASP 密码存储指南](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)：使用适合密码的慢散列。
- [OWASP 会话管理指南](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)：随机会话标识、Cookie 属性、会话生命周期。
- [OWASP CSRF 防护指南](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)：来源及 token 验证，SameSite 的额外防护作用。
