# ebaseline - 智能体基线测试系统

基于 FastAPI 的 Agent 任务完成率测试系统，通过 OpenAI Responses API 格式（`/v1/responses`）调用待测 Agent。

## 快速启动

```bash
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp env.conf.example env.conf  # 修改数据库密码、secret_key
mysql -h 192.168.34.65 -P 3306 -uroot -p'Info@1234' < init.sql
python main.py -f env.conf
```

- Web 控制台: http://localhost:8080/
- API 文档: http://localhost:8080/docs
- MCP 服务: http://localhost:8081/mcp（必须带 `Authorization: Bearer <token>`）
- 初始账号: admin；密码取 `auth.initial_admin_password` / `EBASE_ADMIN_PASSWORD`，
  未配置时首次启动随机生成并在启动日志提示一次（不再使用固定 admin123）
- MCP token：配置 `mcp.auth_token` / `EBASE_MCP_TOKEN`；未配置时自动生成到
  `runtime/mcp_token`（0600），客户端从该文件读取

## 测试

```bash
python -m unittest discover -s tests -v
```

测试使用标准库 `unittest`，不依赖 MySQL（CRUD/manifest 用 SQLite 内存库），
覆盖依赖图、指标口径、脱敏、批次清单/attempt、MCP token 认证和脚本进程组取消。

## 项目结构

```
main.py                  # 入口：解析 -f 配置，启动 REST API + MCP 服务
ebaseline/
  config.py              # JSON 配置加载（database/server/mcp/auth/agent/scripts）
  database.py            # SQLAlchemy 引擎/会话工厂
  models.py              # 6 张表 ORM（users/task_group/task_list/subjects/task_batch/task_result）
  schemas.py             # Pydantic 请求/响应模型
  auth.py                # JWT + bcrypt + 权限装饰器（0=普通 1=管理员 2=超管）
  crud.py                # 通用 CRUD
  task_graph.py          # 依赖解析/校验/拓扑排序（缺失、跨组、自依赖、环路）
  manifest.py            # 批次冻结清单（任务定义/组变量/脚本 hash）
  security.py            # 组变量脱敏、编辑回填、日志脱敏
  mcp_auth.py            # MCP Bearer token 认证与只读/管理角色
  agent_client.py        # Agent /v1/responses SSE 流式调用（自动追问 + QA 闭环 + 取消）
  executor.py            # 任务执行引擎（依赖拓扑 → pre_script → Agent → expect → clean）
  runtime.py             # 运行上下文快照（runtime/ebase_runs/.../attempt_N/，供脚本 SDK 读取）
  mcp_server.py          # MCP 服务（FastMCP streamable HTTP，36 个工具，写工具需 admin token）
  routers/               # REST API 路由
    auth.py users.py groups.py tasks.py subjects.py batches.py results.py workspace.py
ebaseline_sdk.py         # 脚本侧 SDK（单文件、纯标准库；读运行上下文，无需安装）
scripts/                 # 全局校验脚本（check_db_node.py 等）
  lib/console_mcp.py     # i2up 控制台 MCP 客户端（复用自 stream-agent）
seeds/                   # 数据导入脚本（import_db2_group.py, sync_db2_all.py）
static/index.html        # 单页 Web 控制台（原生 HTML/JS/CSS，无框架）
workspaces/group_<gid>/  # 组级工作空间（独立 venv + 脚本 + 文件）
```

## 关键设计

### 任务执行流程

```
1. 检查依赖（task_list.depends，仅组内，需全部 result=1；启动前校验环路）
2. 执行 pre_script（失败则跳过；脚本在独立进程组中运行，可取消）
3. 渲染 prompt（替换 {VAR} / {VAR.SUB}，从 task_group.variables）
4. 调用 Agent /v1/responses（SSE 流式，支持自动追问、QA 闭环和取消）
5. 执行 expect 校验脚本（返回码 0=成功）
6. 执行 clean 脚本（finally 中执行，无论成败；失败会覆盖为 FAIL）
7. 写 task_result（result: 0=未执行 1=成功 2=失败 3=执行中；带 attempt）
```

### 结果细分状态

- `result_detail=3`：执行成功但未配置 expect 校验 → verdict `UNVALIDATED`（未校验）
- `result_detail=2`：完全成功（Agent + 校验都通过）→ verdict `PASS`
- `result_detail=1`：校验通过但 Agent 异常（Agent 可能已干完活但响应超时）→ `PARTIAL`
- `result_detail=0`：完全失败 → `FAIL`

批次指标区分 `strict_pass_rate = PASS/(PASS+FAIL)`、`pass_rate = PASS/已执行`、
`validation_coverage = 已校验/已执行`；没有校验脚本的任务不再计入质量通过率。

### 批次清单与 attempt

- 创建批次时把任务定义、组变量、脚本 sha256 冻结到 `task_batch.manifest`
- 整批重跑使用 manifest 中的定义，`task_batch.attempt` +1，历史结果不删除
- 批次详情默认看最新 attempt，`?attempt=N` 查看历史轮次；`/batches/{id}/attempts`
  返回每轮指标；CSV 导出支持 `attempt` 过滤
- 单任务调试使用当前任务定义，与 manifest 不一致时在 metrics 标记
  `definition_mismatch=true`

### 工作空间

- 每个任务组有独立目录 `workspaces/group_<gid>/`
- 创建组时选择 Python 版本（3.9/3.11/3.14），首次访问时懒创建独立 venv
- 脚本优先从组工作空间解析，找不到再回退到全局 `scripts/`
- 组变量自动注入为环境变量（如 `DB2_NODE_SRC_IP`）
- Web 终端（WebSocket + pty）cwd 固定在工作空间，PATH 前置组 venv
- 终端默认仅 privilege >= 1 可用（`workspace.terminal_min_privilege`）；服务以
  root 运行时默认拒绝（`workspace.allow_root_execution=false`），避免继承 root 身份
- `/api/v1/workspace/capabilities` 返回当前用户能否使用终端及原因，前端据此显示/禁用

### 运行上下文与脚本 SDK

- 每个任务在每个阶段把快照原子写入
  `runtime/ebase_runs/group_<gid>/batch_<bid>/attempt_<n>/<tid>.json`
  （`<tid>.qa.json` 存 QA 闭环记录；`runtime/` 已加入 `.gitignore`；
  每组保留 20 个批次、每批次保留 20 个 attempt）
- 执行器注入 `EBASE_RUN_FILE` / `EBASE_RUN_DIR` / `EBASE_BATCH_ID` / `EBASE_GID` /
  `EBASE_TID` / `EBASE_STAGE` / `EBASE_RUNTIME_ROOT`，并把项目根加入 `PYTHONPATH`
- 工作空间脚本 `import ebaseline_sdk as eb`，可用
  `agent_output()` / `agent()` / `qa_record()` / `stage()` / `task()` /
  `variable()` / `result()` / `agent_output(tid)`（读上游任务输出）/ `results()` / `report()`
- `expect` 阶段仍向 stdin 写 Agent 回答，旧脚本零改动兼容
- 手工调试：Web 终端里 `EBASE_GID` 已注入，SDK 退化为读该组最近一次执行；
  `python ebaseline_sdk.py --dump` 可查看完整快照
- 完整 API 文档：`docs/ebaseline_sdk.md`

### 用户账户与个人 API Key

- Web 控制台「用户管理」：所有登录用户可修改自己的用户名/邮箱/密码（需当前密码），
  生成/重新生成/撤销个人 MCP API Key；管理员额外看到用户列表和权限调整。
- `users.api_key_hash` 只存 SHA-256；`api_key_prefix` 用于页面展示；
  `api_key_created_at` / `api_key_last_used_at` 记录生命周期。
- API Key 角色由用户 `privilege` 决定：0=只读，>=1=可写；重新生成会使旧 Key 失效。
- 管理员撤销他人 Key：等级 1 只能处理非超管，等级 2 可处理所有用户。

### MCP 工具

任务组/任务：`list_task_groups`, `get_task_group`, `create_task_group`, `update_task_group`, `delete_task_group`, `list_tasks_in_group`, `get_task_detail`, `render_task_prompt`, `create_task`, `update_task`, `delete_task`

待测主体：`list_subjects`, `get_subject`, `create_subject`, `update_subject`, `delete_subject`

批次/结果：`create_batch`, `list_batches`, `start_batch`, `get_batch_status`, `get_batch_detail`, `run_batch_task`, `cancel_batch`, `delete_batch`, `get_task_results`, `submit_task_result`, `export_batch_csv`, `list_results`, `get_result`, `delete_results`, `export_results_csv`

工作空间：`list_workspace_files`, `read_workspace_file`, `write_workspace_file`, `make_workspace_dir`, `delete_workspace_path`

认证/用户管理和 WebSocket 终端不暴露为 MCP 工具（前者缺少请求级用户上下文，后者依赖长连接）。

MCP 认证：
- 所有 HTTP 请求必须带 `Authorization: Bearer <token>`，中间件在工具调用前校验
- **个人 API Key**：Web 控制台「用户管理」生成，格式 `ebk_...`；数据库存 SHA-256，
  明文只返回一次；角色按用户等级映射（0=只读，>=1=可写），重新生成/撤销立即生效
- **服务级 token**：`mcp.auth_token` = admin（全部工具）；
  `mcp.readonly_token` = 只读（list/get/export；写工具、`render_task_prompt` 被拒绝）
- 服务级 token 未配置时自动生成到 `mcp.token_file`（默认 `runtime/mcp_token`，0600）
- 跨机访问还需配置 `mcp.allowed_hosts` / `mcp.allowed_origins`（SDK 传输层白名单）

## 数据库

- MySQL `ebase` schema，仅使用该 schema，不修改其他 schema
- `task_list.tid` 非自增，应用层分配（`gid * 10000 + seq`）
- `users` 新增 `api_key_hash` / `api_key_prefix` / `api_key_created_at` / `api_key_last_used_at`
- `task_batch` 新增 `attempt`、`manifest`；`task_result` 新增 `attempt`
- 启动时 `create_all` + `_ensure_columns` 自动补列，并对账上次残留的“执行中”状态
- `init.sql` 中 `subjects` 表有两处语法错误已修复（`secret` 行末缺逗号、`api_mode` 行末多逗号）

## 已知限制

- 前端 xterm.js 从 CDN 加载，离线环境需下载到本地
- QA 闭环用例建议 timeout 设 600s 以上（多轮交互耗时）
- 组 venv 创建后如需切换 Python 版本，需手动删除 `.venv/` 重建
- 脚本/Agent 调用仍以服务进程身份执行；本次只关闭了普通用户到 root 终端的路径，
  彻底隔离仍需独立非 root 账号、容器或沙箱部署
- MCP 使用静态 Bearer token，不是 OAuth；token 轮换需重启服务或改配置

## 相关项目

- `/home/stream-agent/test`：现有 pytest 测试框架，本系统复用了其 `console_mcp.py`、`check_db_node.py`、`run_agent` 逻辑
