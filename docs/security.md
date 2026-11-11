# 安全边界与配置

本文说明 ebaseline 当前版本的访问边界、默认策略和残余风险。它是部署检查清单，
不是完整的威胁模型。

## 1. REST / Web 控制台

- 登录使用 JWT；`auth.secret_key` 必须替换为随机值，且不要提交到版本库。
- 权限等级：0=普通用户，1=管理员，2=超管。
  - 等级 1 可以管理任务组/任务/主体/批次和普通用户，但不能触碰超管、不能授予超管。
  - 只有超管可以降级超管；系统拒绝降级最后一个超管。
- `auth.allow_register` 默认 true（保持兼容）。内部部署建议改为 false。
- `auth.allow_init_admin` 默认 false；公开的 `/api/v1/auth/init-admin` 只在
  “配置放行且库中还没有任何用户”时可用。
- 初始 admin 密码取 `auth.initial_admin_password` / `EBASE_ADMIN_PASSWORD`；
  未配置时随机生成并在首次启动日志提示一次。登录后请立即修改。

## 2. Web 终端

- WebSocket 终端在 `accept()` 前校验 token、用户、任务组、权限等级和 root 策略。
- 默认 `workspace.terminal_min_privilege=1`（仅管理员）。
- 默认 `workspace.allow_root_execution=false`：如果服务进程是 root，终端直接拒绝，
  避免普通用户通过 pty 继承 root 身份。
- 前端通过 `GET /api/v1/workspace/capabilities` 获取当前用户是否可用，不再先连接再失败。
- 文件 API：读需要登录（等级 0），写/建目录/删除需要等级 1；路径解析限制在组工作空间内。

**残余风险**：pre_script/expect/clean 以及 Agent 调用仍以服务进程身份执行。
真正的隔离需要专用非 root 账号、容器或沙箱；本版本没有实现。生产部署不要把服务
直接以 root 运行。

## 3. MCP 服务

- 所有 MCP HTTP 请求必须带 `Authorization: Bearer <token>`；中间件在进入任何工具前
  校验，失败返回 401。
- **服务级 token** 来源优先级：`EBASE_MCP_TOKEN` → `mcp.auth_token` → `mcp.token_file`。
  都没有时自动生成随机 token 到 `runtime/mcp_token`（0600），重启复用。
- `mcp.readonly_token` 可选，提供服务级只读角色：list/get/export 可用；写工具、
  `render_task_prompt` 被拒绝。`render_task_prompt` 会用真实变量渲染，可能带出秘密，
  因此只给 admin。
- **个人 API Key**：登录后在 Web 控制台「用户管理」页生成，格式 `ebk_...`。
  - 明文只在生成响应中返回一次；数据库只保存 SHA-256（`users.api_key_hash`）和展示前缀。
  - 角色由用户等级决定：等级 0 → 只读；等级 ≥1 → 可写。用户被降级后，Key 的写权限
    立即随之变化。
  - 重新生成会立即使旧 Key 失效；用户本人或管理员可以撤销。
  - 管理员撤销他人 Key 时，等级 1 只能处理非超管用户；等级 2 可处理所有用户。
- `mcp.allow_unauthenticated` 默认 false。只有本地临时调试时才建议开启，启动会打印告警。
- 跨机访问还需要配置 `mcp.allowed_hosts` / `mcp.allowed_origins`，否则 SDK 的
  DNS rebinding 白名单会返回 421。不要为了省事长期使用 `["*"]`。
- 默认示例把 `mcp.host` 设为 `127.0.0.1`；如果必须监听 `0.0.0.0`，请配合 token、
  网络 ACL/防火墙和 allowed_hosts。
- API Key 通过 HTTP 传输；正式部署必须在入口启用 HTTPS，否则 Key 可能被链路窃听。

## 4. 组变量中的秘密

- `security.mask_group_secrets=true` 时，REST/MCP 响应会按 key 正则递归脱敏
  （password/passwd/secret/token/api_key/credential/private_key）。
- 更新组变量时，值等于 `security.mask_placeholder` 的字段会保留旧值，避免用户在页面上
  编辑非敏感字段时覆盖真实秘密。
- 执行日志写入数据库前会把秘密值替换为占位符（长度 >= 4 的值）。
- 运行上下文 `runtime/.../attempt_N/<tid>.json` 仍包含真实组变量，供脚本读取；
  该目录已在 `.gitignore` 中，不要整体打包外传。
- 待测主体的 `secret` 从不出现在 REST/MCP 响应中（`SubjectResponse` 不含该字段）。

## 5. 执行与数据

- 脚本在独立进程组中运行；取消/超时会 TERM → KILL 整个进程组。
- clean 在 `finally` 中执行；clean 失败会把任务判为 FAIL，不再显示 PASS/UNVALIDATED。
- 批次重跑使用创建时冻结的 manifest，结果按 attempt 追加；删除批次才会删除该批次全部结果。
- 启动时会把上次进程残留的 `status=1` 批次和 `result=3` 结果标记为中断/失败。
  该对账假设同一 schema 只有一个服务实例；多实例部署需要额外的 lease/心跳机制（未实现）。
- 旧批次没有 manifest 时，详情回退到当前任务定义并明确标注，不能证明创建时的定义。

## 6. 部署检查清单

- [ ] `auth.secret_key` 为随机值，未提交到版本库
- [ ] 修改初始 admin 密码，或设置 `EBASE_ADMIN_PASSWORD`
- [ ] `auth.allow_register=false`（内部场景）
- [ ] 确认 MCP token 已生成/配置，客户端使用 Bearer 认证
- [ ] 用户优先使用「用户管理」生成的个人 API Key，避免共享服务级 token
- [ ] 定期检查用户的 Key 前缀/最近使用时间，离职或异常时撤销
- [ ] MCP 只监听必要地址；跨机访问配置 allowed_hosts 和防火墙
- [ ] 服务以非 root 账号运行；如需 root 终端必须显式开启并理解风险
- [ ] 组变量中的秘密字段命名能命中脱敏规则
- [ ] 生产入口使用 HTTPS（本应用本身不终止 TLS）
