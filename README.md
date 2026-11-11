# ebaseline - 智能体基线测试系统

基于 OpenAI Responses API 格式的 Agent 任务完成率测试系统。

## 功能特性

- **REST API**：用户管理、任务组/任务 CRUD、批次执行、结果查询与导出
- **MCP 服务**：智能体可通过 MCP 协议操作测试任务（拉取任务、上报结果），
  必须携带 Bearer token；写工具仅 admin 角色可用
- **任务执行引擎**：支持前置脚本、Agent 调用、期望校验、清理脚本四阶段，
  脚本在独立进程组中运行，可取消、超时整组回收
- **依赖拓扑排序**：保存/执行前校验缺失、跨组、自依赖和环路，拒绝非法依赖图
- **变量替换**：支持 `{VAR}` 和 `{VAR.SUBVAR}` 占位符；组变量中的秘密字段在
  API/页面/日志中脱敏
- **QA 闭环**：支持多轮对话自动应答（conversation 字段）
- **权限控制**：三级权限（0=普通用户，1=管理员，2=超级管理员）；
  Web 终端默认仅管理员可用，root 服务默认禁用
- **用户管理**：登录用户可自助修改用户名/邮箱/密码，生成或撤销个人
  MCP API Key；管理员可查看用户列表、调整权限、撤销他人 Key
- **可追溯评测**：批次创建时冻结任务/变量/脚本 hash（manifest）；重跑追加
  attempt 而不覆盖历史；无校验脚本的执行成功标记为“未校验”而不是“通过”

## 快速开始

### 1. 安装依赖

```bash
cd /home/github/ebaseline
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置

复制配置文件模板并修改：

```bash
cp env.conf.example env.conf
# 编辑 env.conf，修改数据库密码、secret_key 等
```

### 3. 初始化数据库

```bash
# 确保 MySQL 可访问，然后执行：
mysql -h 192.168.34.65 -P 3306 -uroot -p'Info@1234' < init.sql
```

### 4. 启动服务

```bash
source venv/bin/activate
python main.py -f env.conf
```

启动后：
- **Web 控制台（主页）**: http://0.0.0.0:8080/ — 普通用户的操作入口（登录后可视化管理任务组/任务/待测主体/批次/结果）
- REST API 文档（Swagger）: http://0.0.0.0:8080/docs
- MCP 服务（供智能体接入）: http://127.0.0.1:8081/mcp（需 Bearer token）

初始管理员账号为 `admin`：密码取 `auth.initial_admin_password` / `EBASE_ADMIN_PASSWORD`，
未配置时首次启动随机生成并在启动日志提示一次。MCP token 未配置时自动生成到
`runtime/mcp_token`（0600），客户端需设置 `Authorization: Bearer <文件内容>`。
终端/MCP/组变量的安全边界见 [docs/security.md](docs/security.md)。

Web 控制台功能：
- **任务组**：列表、新建/编辑（含 JSON 变量编辑）、删除、进入任务列表
- **任务列表**：查看任务详情（含 input prompt 模板）、新建/编辑/删除、组变量查看
- **待测主体**：注册/编辑/删除 Agent（地址、端口、API Key、API 模式）
- **测试批次**：创建批次（选 Agent + 任务组）、执行/取消、查看每任务执行状态与日志
- **执行结果**：全量结果列表、按条件过滤、导出 CSV、查看详细日志
- **用户管理**：修改自己的用户名/邮箱/密码；生成/撤销个人 MCP API Key；
  管理员可调整用户权限、撤销他人 Key

### 5. 导入 DB2 测试用例组

```bash
source venv/bin/activate
python seeds/import_db2_group.py
```

## 项目结构

```
ebaseline/
├── main.py                  # 启动入口
├── ebaseline/               # 核心包
│   ├── config.py            # 配置加载
│   ├── database.py          # SQLAlchemy 引擎/会话
│   ├── models.py            # 6 张表的 ORM 模型
│   ├── schemas.py           # Pydantic 请求/响应模型
│   ├── auth.py              # JWT 认证 + bcrypt 密码散列
│   ├── crud.py              # 通用 CRUD
│   ├── task_graph.py        # 依赖校验/拓扑排序
│   ├── manifest.py          # 批次冻结清单（任务/变量/脚本 hash）
│   ├── security.py          # 组变量脱敏与日志脱敏
│   ├── mcp_auth.py          # MCP Bearer token 认证与角色
│   ├── agent_client.py      # Agent /v1/responses SSE 调用（支持取消）
│   ├── executor.py          # 任务执行引擎（异步线程池 + attempt/manifest）
│   ├── runtime.py           # 运行上下文快照（runtime/.../attempt_N/，供 SDK 读取）
│   ├── mcp_server.py        # MCP 服务（FastMCP streamable HTTP + token 认证）
│   └── routers/             # REST API 路由
│       ├── auth.py          # POST /auth/register, /auth/login
│       ├── users.py         # GET/PUT /users
│       ├── groups.py        # CRUD /groups
│       ├── tasks.py         # CRUD /groups/{gid}/tasks, /tasks/{tid}
│       ├── subjects.py      # CRUD /subjects
│       ├── batches.py       # CRUD /batches + 批次/单任务执行
│       └── results.py       # GET /results + GET /results/export
├── ebaseline_sdk.py         # 脚本侧 SDK（单文件、纯标准库，读运行上下文）
├── scripts/                 # 校验脚本
│   ├── lib/console_mcp.py   # i2up 控制台 MCP 客户端
│   └── check_db_node.py     # 数据库节点状态检查
├── seeds/
│   └── import_db2_group.py  # DB2 用例组导入
├── env.conf.example         # 配置文件模板
└── requirements.txt
```

## 核心概念

### 任务组（task_group）

一组相关任务的集合，支持定义共享变量（JSON 格式）。变量在任务执行时通过 `{VAR}` 或 `{VAR.SUBVAR}` 占位符注入 prompt。

### 任务（task_list）

| 字段 | 说明 |
|------|------|
| tid | 任务 ID（主键，格式：gid * 10000 + 序号） |
| gid | 所属任务组 |
| name | 任务名称 |
| depends | 依赖任务 ID 列表（逗号分隔，仅组内） |
| pre_script | 前置脚本路径（相对 scripts/ 目录） |
| input | JSON：{prompt, conversation?} |
| timeout | Agent 调用超时（秒），留空使用全局默认 `agent.default_timeout` |
| expect | 期望校验脚本路径（返回码 0=成功） |
| clean | 清理脚本路径（无论成败都会执行） |

### 待测主体（subjects）

被测试的 Agent 服务地址配置（IP + 端口 + API Key）。

### 批次（task_batch）

一次测试执行的批次记录，包含执行状态（0=未执行，1=执行中，2=已完成，3=失败，
4=部分成功，5=已取消）。创建时把任务定义、组变量和脚本 sha256 冻结到 `manifest`；
整批重跑时 `attempt` +1，使用冻结定义，历史结果不删除。

### 结果（task_result）

每个任务的执行结果（0=未执行，1=执行成功，2=失败，3=执行中）及详细日志。
`result_detail`：3=执行成功但未校验（UNVALIDATED）、2=已校验通过（PASS）、
1=校验通过但 Agent 异常（PARTIAL）、0=失败。`attempt` 标记所属轮次。

## API 概览

### 认证
- `POST /api/v1/auth/register` - 注册
- `POST /api/v1/auth/login` - 登录
- `GET /api/v1/auth/me` - 当前用户（含 API Key 前缀/生成时间/最近使用时间）

### 用户/账户
- `GET /api/v1/users` - 用户列表（需管理员）
- `PUT /api/v1/users/me/profile` - 修改自己的用户名/邮箱（需当前密码）
- `PUT /api/v1/users/me/password` - 修改自己的密码（需当前密码）
- `POST /api/v1/users/me/api-key` - 生成/重新生成个人 MCP API Key（明文仅返回一次）
- `DELETE /api/v1/users/me/api-key` - 撤销自己的 API Key
- `PUT /api/v1/users/{id}/privilege` - 调整用户权限（需管理员）
- `DELETE /api/v1/users/{id}/api-key` - 撤销指定用户的 API Key（需管理员）

### 任务组
- `POST /api/v1/groups` - 创建（需管理员）
- `GET /api/v1/groups` - 列表
- `GET /api/v1/groups/{gid}` - 详情
- `PUT /api/v1/groups/{gid}` - 更新（需管理员）
- `DELETE /api/v1/groups/{gid}` - 删除（需管理员）

### 任务
- `POST /api/v1/groups/{gid}/tasks` - 创建（需管理员）
- `GET /api/v1/groups/{gid}/tasks` - 组内任务列表
- `GET /api/v1/tasks/{tid}` - 任务详情
- `PUT /api/v1/tasks/{tid}` - 更新（需管理员）
- `DELETE /api/v1/tasks/{tid}` - 删除（需管理员）

### 待测主体
- `POST /api/v1/subjects` - 注册（需管理员）
- `GET /api/v1/subjects` - 列表
- `GET /api/v1/subjects/{sid}` - 详情
- `PUT /api/v1/subjects/{sid}` - 更新（需管理员）
- `DELETE /api/v1/subjects/{sid}` - 删除（需管理员）

### 批次
- `POST /api/v1/batches` - 创建
- `GET /api/v1/batches` - 列表
- `GET /api/v1/batches/{id}` - 详情（含任务状态；`?attempt=N` 查看历史轮次）
- `GET /api/v1/batches/{id}/precheck` - 执行前预检（依赖图/未校验任务/脚本缺失）
- `GET /api/v1/batches/{id}/attempts` - 每轮 attempt 的指标
- `POST /api/v1/batches/{id}/run` - 执行（先校验依赖图）
- `POST /api/v1/batches/{id}/tasks/{tid}/run` - 单独执行批次内任务（`?force=true` 忽略依赖）
- `POST /api/v1/batches/{id}/cancel` - 取消（中断正在运行的脚本/Agent 调用）
- `DELETE /api/v1/batches/{id}` - 删除（需管理员）

### 结果
- `GET /api/v1/results` - 查询（支持 sid/gid/tid/result/batch_id/attempt 过滤）
- `GET /api/v1/results/export` - 导出 CSV（支持 `attempt`）

### 工作空间
- `GET /api/v1/workspace/capabilities` - 当前用户的终端可用性
- `GET /api/v1/groups/{gid}/workspace/files|file` - 文件浏览/读取
- `PUT /api/v1/groups/{gid}/workspace/file`、`POST .../mkdir`、`DELETE .../file` - 写入/建目录/删除（需管理员）
- `WS /api/v1/groups/{gid}/workspace/terminal` - Web 终端（默认需管理员且非 root 服务）

## MCP 工具

智能体可通过 MCP 客户端连接 `http://127.0.0.1:8081/mcp`，并在请求头设置
`Authorization: Bearer <token>`。

- **个人 API Key（推荐）**：登录 Web 控制台 →「用户管理」→ 生成 API Key，格式 `ebk_...`。
  明文只显示一次，数据库只存 SHA-256。角色由用户等级决定：等级 0 只能调用只读工具，
  等级 ≥1 可调用写工具；用户被降级后权限立即变化。
- **服务级 token**：`mcp.auth_token` / `EBASE_MCP_TOKEN`，未配置时读取
  `runtime/mcp_token`（首次启动自动生成）；`mcp.readonly_token` 为服务级只读 token。
- 写工具、`render_task_prompt` 需要 admin 角色；`render_task_prompt` 会用真实变量渲染，
  可能带出秘密，因此不对只读 Key 开放。

共 36 个工具：

### 任务组与任务

| 工具 | 说明 |
|------|------|
| `list_task_groups` | 列出所有任务组 |
| `get_task_group` | 获取任务组详情（含变量、Python 版本） |
| `create_task_group` | 创建任务组 |
| `update_task_group` | 更新任务组 |
| `delete_task_group` | 删除任务组（级联清理任务/批次/结果/工作空间） |
| `list_tasks_in_group` | 列出组内任务 |
| `get_task_detail` | 获取任务完整定义 |
| `render_task_prompt` | 用组变量渲染 prompt |
| `create_task` | 创建任务 |
| `update_task` | 更新任务 |
| `delete_task` | 删除任务 |

### 待测主体

| 工具 | 说明 |
|------|------|
| `list_subjects` | 列出待测 agent |
| `get_subject` | 获取待测 agent 详情 |
| `create_subject` | 注册待测 agent |
| `update_subject` | 更新待测 agent |
| `delete_subject` | 删除待测 agent |

### 批次与结果

| 工具 | 说明 |
|------|------|
| `create_batch` | 创建批次 |
| `list_batches` | 列出批次（含聚合指标） |
| `start_batch` | 启动整批执行 |
| `get_batch_status` | 查询批次状态 |
| `get_batch_detail` | 批次详情（指标 + 每任务结果） |
| `run_batch_task` | 单独执行批次内任务（`force` 忽略依赖） |
| `cancel_batch` | 取消正在执行的批次 |
| `delete_batch` | 删除批次 |
| `get_task_results` | 查询批次任务结果 |
| `submit_task_result` | 智能体主动上报结果 |
| `export_batch_csv` | 导出批次结果 CSV |
| `list_results` | 按 sid/gid/tid/result/batch_id 查询结果 |
| `get_result` | 获取单条结果详情（完整日志） |
| `delete_results` | 批量删除结果 |
| `export_results_csv` | 按条件导出结果 CSV |

### 工作空间

| 工具 | 说明 |
|------|------|
| `list_workspace_files` | 列出工作空间目录 |
| `read_workspace_file` | 读取文件内容 |
| `write_workspace_file` | 写入文件 |
| `make_workspace_dir` | 创建目录 |
| `delete_workspace_path` | 删除文件/目录 |

> 认证与用户管理、WebSocket 终端依赖请求级用户上下文或长连接，仅通过 REST/Web 控制台提供，不暴露为 MCP 工具。

## 任务执行流程

```
对于批次中的每个任务（按冻结清单/依赖拓扑排序）：
1. 检查依赖：依赖任务必须全部 result=1（成功）
2. 执行 pre_script（如果配置了；独立进程组，可取消）
3. 渲染 prompt：替换 {VAR} 和 {VAR.SUBVAR} 占位符
4. 调用 Agent API（OpenAI Responses API 格式，SSE 流式）
   - 超时时间取 task_list.timeout，未配置时使用全局默认 agent.default_timeout
   - 自动附加自主执行指令（可在配置关闭）
   - 检测到请求确认时自动追问
   - 支持 conversation 多轮 QA 闭环
   - 取消信号可中断流式读取
5. 执行 expect 校验脚本（返回码 0=成功；未配置则结果为“未校验”）
6. 执行 clean 清理脚本（finally 中执行；失败会把该任务判为 FAIL）
7. 写 task_result 到数据库（带 attempt；重跑追加，不覆盖历史）
```

批次状态与判定分离：批次状态描述“执行是否走完”，任务 verdict 描述“质量是否通过”。
无 expect 的任务即使 Agent 执行成功也只算 `UNVALIDATED`，不计入严格通过率。

## 变量替换

任务组中定义变量：
```json
{
  "NODE_NAME": "el7",
  "DB2_NODE_SRC": {
    "ip": "192.168.34.8",
    "port": "50000"
  }
}
```

任务 prompt 中使用：
```
帮我创建 DB2 节点，机器节点选择 {NODE_NAME}，IP 是 {DB2_NODE_SRC.ip}
```

执行时自动替换为实际值。

## 脚本开发

校验脚本放在 `scripts/` 目录下，需满足：
- 可执行（或指定解释器：`.py` 用 python3，`.sh` 用 bash）
- 返回码 0 表示成功，非 0 表示失败
- 输出 JSON 到 stdout（可选，便于解析）

系统会自动将任务组变量拍平为环境变量注入脚本进程，例如：
- `DB2_NODE_SRC.ip` → `DB2_NODE_SRC_IP=192.168.34.8`

### 用 SDK 读取 Agent 输出（推荐）

执行器把每个任务的运行上下文写进
`runtime/ebase_runs/group_<gid>/batch_<bid>/attempt_<n>/<tid>.json`，
脚本通过 `ebaseline_sdk`（单文件、纯标准库、无需在组 venv 里安装）直接读取：

```python
import ebaseline_sdk as eb

eb.agent_output()               # 当前任务 Agent 的完整回答
eb.agent()                      # success/error/rounds/tokens/tool_calls/qa_record
eb.qa_record()                  # QA 闭环记录（探测到/喂回的字段）
eb.stage()                      # pre_script / agent / expect / clean / done
eb.task()                       # tid/gid/name/depends/...
eb.variable("DB2_NODE_SRC.ip")  # 组变量按路径取值
eb.result()                     # 当前任务结果；未收尾时返回 pending/RUNNING
eb.agent_output(dep_tid)        # 读取本批次上游任务的输出
eb.result(dep_tid)              # 读取上游任务结果
eb.results()                    # 本批次所有任务的执行摘要
eb.report(ok=True, detail={...})  # 输出 JSON 并按 ok 设置退出码
```

要点：
- 执行器注入 `EBASE_RUN_FILE`、`EBASE_RUN_DIR`、`EBASE_TID`、`EBASE_STAGE` 等环境变量，
  并把项目根加入 `PYTHONPATH`，脚本无需配置即可 `import ebaseline_sdk`
- 在 Web 终端里手工调试时退化为「该组最近一次执行」的上下文，
  也可直接运行 `python ebaseline_sdk.py --dump` 查看完整快照
- `expect` 阶段仍会同时通过 stdin 传入 Agent 回答，旧脚本无需改动
- 上下文文件不含待测主体的 API key，但**包含组变量**（可能含密码），勿随意外传

完整的 API 说明、上下文 JSON 结构与故障排查见 [docs/ebaseline_sdk.md](docs/ebaseline_sdk.md)。

## 配置文件

```json
{
  "database": {
    "host": "192.168.34.65",
    "port": 3306,
    "user": "root",
    "password": "Info@1234",
    "schema": "ebase"
  },
  "server": {"host": "0.0.0.0", "port": 8080},
  "mcp": {
    "enabled": true, "host": "127.0.0.1", "port": 8081, "path": "/mcp",
    "auth_token": "", "readonly_token": "",
    "token_file": "runtime/mcp_token",
    "allow_unauthenticated": false,
    "allowed_hosts": [], "allowed_origins": []
  },
  "workspace": {
    "terminal_enabled": true,
    "terminal_min_privilege": 1,
    "allow_root_execution": false
  },
  "security": {"mask_group_secrets": true, "mask_placeholder": "********"},
  "auth": {
    "secret_key": "change-me", "token_expire_minutes": 1440,
    "allow_register": true, "allow_init_admin": false,
    "initial_admin_password": ""
  },
  "agent": {"default_timeout": 300, "max_follow_ups": 3, "autonomy_directive": true},
  "scripts": {"base_dir": "./scripts", "shell": "/bin/bash"}
}
```

`mcp.allowed_hosts` / `allowed_origins` 用于跨机访问时放开 SDK 的 Host/Origin 白名单；
默认只允许 localhost。`workspace.allow_root_execution` 默认 false：以 root 运行时
Web 终端会被拒绝，必须显式开启或改用非 root 账号/容器运行。

## 测试

```bash
source venv/bin/activate
python -m unittest discover -s tests -v
```

测试使用标准库 `unittest`，不需要 MySQL 或真实 Agent：CRUD/manifest 用 SQLite
内存库，脚本取消用本地进程验证。安全配置与部署检查见 [docs/security.md](docs/security.md)。

## 与现有测试框架的关系

本系统复用了 `/home/stream-agent/test` 的核心逻辑：
- `agent_client.py` 复用了 `test_i2stream.py` 的 `run_agent` SSE 调用逻辑
- `check_db_node.py` 复用了 `check_scripts/i2stream/check_db_node.py`
- `console_mcp.py` 复用了 `lib/console_mcp.py`
- DB2 用例组从 `input/operate_db_node/db2/` 导入

主要区别：
- 本系统是 **Web 服务**，支持多人协作和持久化存储
- 现有框架是 **pytest 脚本**，适合单次执行
- 本系统提供 **MCP 接口**，智能体可主动拉取任务
