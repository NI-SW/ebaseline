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
- MCP 服务: http://localhost:8081/mcp
- 默认账号: admin / admin123

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
  agent_client.py        # Agent /v1/responses SSE 流式调用（自动追问 + QA 闭环）
  executor.py            # 任务执行引擎（依赖拓扑排序 → pre_script → Agent → expect → clean）
  mcp_server.py          # MCP 服务（FastMCP streamable HTTP，12 个工具）
  routers/               # REST API 路由
    auth.py users.py groups.py tasks.py subjects.py batches.py results.py workspace.py
scripts/                 # 全局校验脚本（check_db_node.py 等）
  lib/console_mcp.py     # i2up 控制台 MCP 客户端（复用自 stream-agent）
seeds/                   # 数据导入脚本（import_db2_group.py, sync_db2_all.py）
static/index.html        # 单页 Web 控制台（原生 HTML/JS/CSS，无框架）
workspaces/group_<gid>/  # 组级工作空间（独立 venv + 脚本 + 文件）
```

## 关键设计

### 任务执行流程

```
1. 检查依赖（task_list.depends，仅组内，需全部 result=1）
2. 执行 pre_script（失败则跳过）
3. 渲染 prompt（替换 {VAR} / {VAR.SUB}，从 task_group.variables）
4. 调用 Agent /v1/responses（SSE 流式，支持自动追问和 QA 闭环）
5. 执行 expect 校验脚本（返回码 0=成功）
6. 执行 clean 脚本（无论成败）
7. 写 task_result（result: 0=未执行 1=成功 2=失败 3=执行中）
```

### 结果细分状态

- `result_detail=2`：完全成功（Agent + 校验都通过）
- `result_detail=1`：校验通过但 Agent 异常（Agent 可能已干完活但响应超时）
- `result_detail=0`：完全失败

### 工作空间

- 每个任务组有独立目录 `workspaces/group_<gid>/`
- 创建组时选择 Python 版本（3.9/3.11/3.14），首次访问时懒创建独立 venv
- 脚本优先从组工作空间解析，找不到再回退到全局 `scripts/`
- 组变量自动注入为环境变量（如 `DB2_NODE_SRC_IP`）
- Web 终端（WebSocket + pty）cwd 固定在工作空间，PATH 前置组 venv

### MCP 工具

`list_task_groups`, `get_task_group`, `list_tasks_in_group`, `get_task_detail`, `render_task_prompt`, `list_subjects`, `create_batch`, `start_batch`, `get_batch_status`, `get_task_results`, `submit_task_result`, `export_batch_csv`

## 数据库

- MySQL `ebase` schema，仅使用该 schema，不修改其他 schema
- `task_list.tid` 非自增，应用层分配（`gid * 10000 + seq`）
- `init.sql` 中 `subjects` 表有两处语法错误已修复（`secret` 行末缺逗号、`api_mode` 行末多逗号）

## 已知限制

- 前端 xterm.js 从 CDN 加载，离线环境需下载到本地
- QA 闭环用例建议 timeout 设 600s 以上（多轮交互耗时）
- 组 venv 创建后如需切换 Python 版本，需手动删除 `.venv/` 重建

## 相关项目

- `/home/stream-agent/test`：现有 pytest 测试框架，本系统复用了其 `console_mcp.py`、`check_db_node.py`、`run_agent` 逻辑
