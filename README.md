# ebaseline - 智能体基线测试系统

基于 OpenAI Responses API 格式的 Agent 任务完成率测试系统。

## 功能特性

- **REST API**：用户管理、任务组/任务 CRUD、批次执行、结果查询与导出
- **MCP 服务**：智能体可直接通过 MCP 协议操作测试任务（拉取任务、上报结果）
- **任务执行引擎**：支持前置脚本、Agent 调用、期望校验、清理脚本四阶段
- **依赖拓扑排序**：自动按任务依赖关系排序执行
- **变量替换**：支持 `{VAR}` 和 `{VAR.SUBVAR}` 占位符，从任务组变量中取值
- **QA 闭环**：支持多轮对话自动应答（conversation 字段）
- **权限控制**：三级权限（0=普通用户，1=管理员，2=超级管理员）

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
- MCP 服务（供智能体接入）: http://0.0.0.0:8081/mcp

默认管理员账号：`admin` / `admin123`

Web 控制台功能：
- **任务组**：列表、新建/编辑（含 JSON 变量编辑）、删除、进入任务列表
- **任务列表**：查看任务详情（含 input prompt 模板）、新建/编辑/删除、组变量查看
- **待测主体**：注册/编辑/删除 Agent（地址、端口、API Key、API 模式）
- **测试批次**：创建批次（选 Agent + 任务组）、执行/取消、查看每任务执行状态与日志
- **执行结果**：全量结果列表、按条件过滤、导出 CSV、查看详细日志

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
│   ├── agent_client.py      # Agent /v1/responses SSE 调用
│   ├── executor.py          # 任务执行引擎（异步线程池）
│   ├── mcp_server.py        # MCP 服务（FastMCP streamable HTTP）
│   └── routers/             # REST API 路由
│       ├── auth.py          # POST /auth/register, /auth/login
│       ├── users.py         # GET/PUT /users
│       ├── groups.py        # CRUD /groups
│       ├── tasks.py         # CRUD /groups/{gid}/tasks, /tasks/{tid}
│       ├── subjects.py      # CRUD /subjects
│       ├── batches.py       # CRUD /batches + POST /batches/{id}/run
│       └── results.py       # GET /results + GET /results/export
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
| expect | 期望校验脚本路径（返回码 0=成功） |
| clean | 清理脚本路径（无论成败都会执行） |

### 待测主体（subjects）

被测试的 Agent 服务地址配置（IP + 端口 + API Key）。

### 批次（task_batch）

一次测试执行的批次记录，包含执行状态（0=未执行，1=执行中，2=已完成，3=失败，4=部分成功）。

### 结果（task_result）

每个任务的执行结果（0=未执行，1=成功，2=失败）及详细日志。

## API 概览

### 认证
- `POST /api/v1/auth/register` - 注册
- `POST /api/v1/auth/login` - 登录
- `GET /api/v1/auth/me` - 当前用户

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
- `GET /api/v1/batches/{id}` - 详情（含任务状态）
- `POST /api/v1/batches/{id}/run` - 执行
- `POST /api/v1/batches/{id}/cancel` - 取消
- `DELETE /api/v1/batches/{id}` - 删除（需管理员）

### 结果
- `GET /api/v1/results` - 查询（支持 sid/gid/tid/result 过滤）
- `GET /api/v1/results/export` - 导出 CSV

## MCP 工具

智能体可通过 MCP 客户端连接 `http://<host>:8081/mcp` 使用以下工具：

| 工具 | 说明 |
|------|------|
| `list_task_groups` | 列出所有任务组 |
| `get_task_group` | 获取任务组详情（含变量） |
| `list_tasks_in_group` | 列出组内任务 |
| `get_task_detail` | 获取任务完整定义 |
| `render_task_prompt` | 用组变量渲染 prompt |
| `list_subjects` | 列出待测 agent |
| `create_batch` | 创建批次 |
| `start_batch` | 启动批次执行 |
| `get_batch_status` | 查询批次状态 |
| `get_task_results` | 查询批次任务结果 |
| `submit_task_result` | 智能体主动上报结果 |
| `export_batch_csv` | 导出批次结果 CSV |

## 任务执行流程

```
对于批次中的每个任务（按依赖拓扑排序）：
1. 检查依赖：依赖任务必须全部 result=1（成功）
2. 执行 pre_script（如果配置了）
3. 渲染 prompt：替换 {VAR} 和 {VAR.SUBVAR} 占位符
4. 调用 Agent API（OpenAI Responses API 格式，SSE 流式）
   - 自动附加自主执行指令（可在配置关闭）
   - 检测到请求确认时自动追问
   - 支持 conversation 多轮 QA 闭环
5. 执行 expect 校验脚本（返回码 0=成功）
6. 执行 clean 清理脚本（无论成败）
7. 写 task_result 到数据库
```

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
  "mcp": {"enabled": true, "host": "0.0.0.0", "port": 8081, "path": "/mcp"},
  "auth": {"secret_key": "change-me", "token_expire_minutes": 1440},
  "agent": {"default_timeout": 300, "max_follow_ups": 3, "autonomy_directive": true},
  "scripts": {"base_dir": "./scripts", "shell": "/bin/bash"}
}
```

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
