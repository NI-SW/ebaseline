# ebaseline SDK 使用文档

`ebaseline_sdk` 让工作空间内的 Python 脚本直接读取当前任务的运行上下文——
Agent 的完整回答、QA 闭环记录、组变量、当前阶段、上游任务输出等，
不必再靠 stdin 传参，也不用连数据库或调 REST API。

- **零安装**：单个 `.py` 文件、纯标准库，组独立 venv 里无需 `pip install`
- **零配置**：执行器通过环境变量注入上下文位置，脚本只管 `import`
- **只读**：不联网、不连库，只读本地 JSON 快照
- **向后兼容**：`expect` 阶段仍会通过 stdin 传入 Agent 回答，旧脚本无需改动

适用版本：Python 3.9 / 3.11 / 3.14（已在三个版本上验证）。

---

## 目录

1. [快速开始](#1-快速开始)
2. [运行上下文](#2-运行上下文)
3. [执行环境](#3-执行环境)
4. [API 参考](#4-api-参考)
5. [上下文 JSON 结构](#5-上下文-json-结构)
6. [常见用法](#6-常见用法)
7. [手工调试](#7-手工调试)
8. [限制与安全](#8-限制与安全)
9. [故障排查](#9-故障排查)

---

## 1. 快速开始

在工作空间的校验脚本里：

```python
#!/usr/bin/env python3
import ebaseline_sdk as eb

output = eb.agent_output()               # Agent 的完整回答

ok = "创建成功" in output
eb.report(ok, detail={"output_chars": len(output)})
```

`eb.report()` 会打印统一格式的 JSON，并按 `ok` 设置退出码（0=PASS，1=FAIL），
直接满足 `expect` 脚本「返回码 0 表示成功」的约定。

不需要在脚本里做 `sys.path` 处理：执行器会把项目根加入 `PYTHONPATH`，
因此任何组工作空间里的脚本都能直接 `import ebaseline_sdk`。

```
EBASE_RUN_FILE=/home/.../runtime/ebase_runs/group_7/batch_9/70001.json
EBASE_STAGE=expect
```

---

## 2. 运行上下文

### 2.1 目录布局

执行器为每个任务在每个阶段写一份快照：

```
runtime/
└── ebase_runs/
    └── group_<gid>/
        └── batch_<bid>/
            ├── <tid>.json        # 任务运行上下文
            └── <tid>.qa.json     # QA 闭环记录（仅当任务配置了 conversation）
```

- 路径由 `ebaseline/runtime.py` 统一管理，根目录 = 项目根下的 `runtime/`
- `runtime/` 已加入 `.gitignore`，且位于工作空间之外，不会通过
  `/api/v1/groups/{gid}/workspace/file` 被读取
- 写入方式为 **tmp + `os.replace` 原子替换**，脚本不会读到半截 JSON
- 同一批次内的任务串行执行，不同批次目录隔离，因此无需加锁

### 2.2 生命周期

任务开始即创建快照，之后每个阶段覆盖更新一次：

| 时机 | `stage` | 变化 |
|------|---------|------|
| 任务开始（依赖检查中） | `start` | 写入任务定义、组变量 |
| 前置脚本运行前后 | `pre_script` | 记录 `pre_script.returncode/stdout/stderr` |
| prompt 渲染完成 | `render` | 写入 `prompt` |
| Agent 调用完成 | `agent` | 写入 `agent` 全量结果；`qa_record` 另存 `.qa.json` |
| 校验脚本运行前后 | `expect` | 记录 `expect.*` |
| 清理脚本运行前后 | `clean` | 记录 `clean.*` |
| 任务结束 | `done` | 写入最终 `result` |

任务提前结束时同样落到 `done`，终止原因保存在 `result.stage`：
`dependency`（依赖不满足）、`pre_script`（前置失败）、`parse_input`（input JSON 非法）。

### 2.3 清理策略

| 操作 | 行为 |
|------|------|
| 批次重跑 | 删除该批次上下文目录后重建 |
| 每个组 | 只保留最近 20 个批次目录，按修改时间淘汰 |
| 删除任务组 | 删除 `runtime/ebase_runs/group_<gid>/` 全部内容 |

---

## 3. 执行环境

### 3.1 注入的环境变量

执行器在启动 `pre_script` / `expect` / `clean` 子进程时注入：

| 变量 | 说明 |
|------|------|
| `EBASE_RUN_FILE` | 当前任务上下文 JSON 的绝对路径 |
| `EBASE_RUN_DIR` | 当前批次上下文目录 |
| `EBASE_BATCH_ID` | 批次 id |
| `EBASE_SID` | 待测主体 id |
| `EBASE_GID` | 任务组 id |
| `EBASE_TID` | 任务 id |
| `EBASE_STAGE` | 当前阶段（`pre_script` / `expect` / `clean`） |
| `EBASE_RUNTIME_ROOT` | `runtime/` 根目录绝对路径 |
| `EBASE_WORKSPACE` | 组工作空间目录绝对路径 |
| `EBASE_QA_FILE` | QA 记录路径（仅当已产生 qa_record） |
| `I2STREAM_AGENT_QA_FILE` | 同上，兼容旧框架脚本 |
| `PYTHONPATH` | 项目根被前置，用于 `import ebaseline_sdk` |

此外，组变量会拍平为环境变量注入（与上下文内容一致）：

```
NODE_NAME=el7
DB2_SRC={"name": "SOUR_UTF", "port": "50000", ...}    # 列表按 JSON
DB2_SRC_IP=192.168.34.8                              # 嵌套 dict 逐层加下划线
```

### 3.2 阶段与 stdin

| 阶段 | 有上下文 | stdin 内容 |
|------|----------|-----------|
| `pre_script` | 是（无 `agent` 段） | 空 |
| `expect` | 是（含 `agent` 段） | **Agent 完整回答**（旧契约保留） |
| `clean` | 是（含 `agent` 段） | 空 |

所以在 `expect` 脚本里，`eb.agent_output()` 与 `sys.stdin.read()` 的内容**完全一致**，
可以任选其一；新脚本推荐用 SDK，便于同时拿到元数据与上游任务输出。

---

## 4. API 参考

```python
import ebaseline_sdk as eb
```

### 4.1 异常

| 异常 | 基类 | 触发场景 |
|------|------|----------|
| `SDKError` | `RuntimeError` | 所有 SDK 错误的基类 |
| `ContextUnavailable` | `SDKError` | 解析不到上下文（未执行器运行 / 该组无执行记录 / 文件缺失） |
| `AgentOutputUnavailable` | `SDKError` | 上下文存在，但当前任务还没跑到 Agent 阶段 |
| `UnknownTask` | `SDKError` | 本批次里没有指定 tid 的上下文文件 |

`eb.agent()` / `eb.result()` / `eb.qa_record()` 等**查询类**函数在"暂不可用"时
返回空值而非抛异常；需要严格断言时用 `agent_output()` 和显式判断。

### 4.2 上下文定位

#### `context_path() -> Path`

解析当前任务上下文文件路径。优先级：

1. `EBASE_RUN_FILE`
2. `EBASE_RUN_DIR` + `EBASE_TID`
3. `EBASE_GID`（或从 `EBASE_WORKSPACE` 的 `group_<gid>` 推断）→ 该组**最近一次执行**
4. 都没有 → 抛 `ContextUnavailable`

```python
print(eb.context_path())
# /home/github/ebase_dev/ebaseline/runtime/ebase_runs/group_7/batch_9/70001.json
```

#### `available() -> bool`

当前是否能解析到**可读的**上下文文件。不抛异常，适合脚本开头做能力探测。

```python
if not eb.available():
    print("不在 ebaseline 执行环境中，跳过")
    raise SystemExit(0)
```

### 4.3 上下文读取

#### `context() -> dict`

当前任务的完整上下文（详见[第 5 节](#5-上下文-json-结构)）。

```python
ctx = eb.context()
print(ctx["gid"], ctx["tid"], ctx["stage"])
```

#### `context_for(tid: int) -> dict`

本批次内指定任务的上下文，用于读取上游任务。跨批次不支持。

```python
upstream = eb.context_for(40001)
```

#### `metadata() -> dict`

批次级元信息：

```python
{
    "batch_id": 9,
    "sid": 6,
    "gid": 7,
    "tid": 70001,
    "stage": "expect",
    "updated_at": "2026-11-04T03:58:21",
}
```

#### `stage() -> str`

当前阶段。脚本里看到的值：

- `pre_script` — 前置脚本中
- `expect` — 校验脚本中
- `clean` — 清理脚本中
- `done` — 任务已结束（事后读取上下文时）
- `agent` / `render` / `start` — 通常只在执行器内部瞬间存在

```python
assert eb.stage() == "expect"
```

#### `task() -> dict`

当前任务定义：

```python
{
    "tid": 70001,
    "gid": 7,
    "name": "SDK 主任务",
    "description": "",
    "depends": [],
    "pre_script": "check_scripts/sdk_pre.py",
    "expect": "check_scripts/sdk_expect.py",
    "clean": "check_scripts/sdk_clean.py",
    "timeout": None,
}
```

#### `prompt() -> str`

本次实际发送给 Agent 的 prompt（组变量已渲染）。在 `pre_script` 阶段为空字符串。

#### `variables() -> dict`

任务组变量（原始嵌套结构）。

```python
{"DB2_SRC": {"name": "SOUR_UTF", "pwd": "Info@1234", "port": "50000"}}
```

> 可能包含密码等敏感信息，不要打印到会被长期保存的日志里。

#### `variable(path: str, default=...) -> Any`

按点号路径取组变量，找不到时回退同名环境变量（大写、`.` → `_`），
再找不到时返回 `default`；未提供 `default` 则抛 `KeyError`。

```python
eb.variable("DB2_SRC.name")            # "SOUR_UTF"
eb.variable("DB2_SRC.port", "50000")   # 带默认值
eb.variable("NODE_NAME")               # 顶层变量
```

### 4.4 Agent 结果

#### `agent(tid: int | None = None) -> dict`

Agent 结果段；尚未调用 Agent 时返回 `{}`。

```python
{
    "success": True,
    "output": "已完成：……",
    "error": "",
    "returncode": 0,
    "duration_sec": 12.3,
    "rounds": 2,
    "tokens": {"input_tokens": 11, "output_tokens": 22, "total_tokens": 33},
    "usage_seen": True,
    "tool_calls": 5,
    "tool_names": ["create_db_node", "list_db_status"],
    "first_token_ms": 812,
    "qa_record": {...} | None,
}
```

> `usage_seen=False` 表示服务端没有返回 usage，此时 `tokens` 的 0 值不可信，
> 做统计时应跳过而不是当作 0 计入。

#### `agent_output(tid: int | None = None) -> str`

Agent 的完整回答文本。**当前任务尚未到 Agent 阶段时抛 `AgentOutputUnavailable`**，
这是它与 `agent().get("output")` 的主要区别。

```python
text = eb.agent_output()          # 当前任务
upstream = eb.agent_output(40001) # 本批次上游任务
```

#### `qa_record(tid: int | None = None) -> dict | None`

QA 闭环记录。依次尝试：

1. 上下文里的 `agent.qa_record`
2. `EBASE_QA_FILE` 指向的文件
3. `I2STREAM_AGENT_QA_FILE` 指向的文件（旧框架兼容）

都没有时返回 `None`。

```python
{
    "requested_fields": ["password"],
    "asked_rounds": 1,
    "detected_fields": ["password"],
    "fed_fields": ["password"],
}
```

#### `result(tid: int | None = None) -> dict`

任务结果摘要。任务收尾后返回执行器最终结论：

```python
{
    "ok": True,
    "result": 1,
    "result_detail": 2,
    "verdict": "PASS",
    "agent_success": True,
    "expect_success": True,
    "metrics": {...},
    "duration_sec": 12.4,
}
```

若任务仍在执行（例如正在 `expect` 阶段），返回**临时摘要**，`pending=True`，
`verdict` 为 `RUNNING` 或 `PENDING`，避免脚本拿到空 dict 后误判：

```python
{
    "pending": True,
    "ok": None,
    "result": None,
    "result_detail": None,
    "verdict": "RUNNING",
    "agent_success": True,
    "expect_success": None,
    "metrics": None,
}
```

`verdict` 取值：`PASS` / `PARTIAL` / `FAIL` / `SKIP` / `ERROR` / `RUNNING` / `PENDING`。

#### `results() -> dict`

本批次所有任务的摘要，键为 `int` 类型的 tid：

```python
{
    70001: {"stage": "done", "ok": True,  "verdict": "PASS"},
    70002: {"stage": "done", "ok": True,  "verdict": "PASS"},
    70003: {"stage": "done", "ok": True,  "verdict": "PASS"},
}
```

下游任务用它一次性判断前置链路是否都通过。

### 4.5 输出与退出

#### `report(ok: bool, detail: dict | None = None, *, exit: bool = True) -> bool`

打印统一格式的校验结论，默认按 `ok` 抛出 `SystemExit(0/1)`。

```python
eb.report(True, detail={"db_node": "el7", "status": "NORMAL"})
```

输出：

```json
{
  "check_result": "PASS",
  "db_node": "el7",
  "status": "NORMAL"
}
```

传 `exit=False` 时只打印不退出，返回 `ok` 原值。

### 4.6 命令行

```bash
python ebaseline_sdk.py            # 摘要：批次、任务、阶段、Agent 概览与输出预览
python ebaseline_sdk.py --dump     # 完整上下文 JSON
python ebaseline_sdk.py 40001      # 查看本批次指定任务
```

退出码：`0` 成功；`2` 无法解析上下文（stderr 给出原因）。

---

## 5. 上下文 JSON 结构

顶层字段（`schema_version=1`）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `schema_version` | int | 结构版本，当前为 1 |
| `batch_id` / `sid` / `gid` / `tid` | int | 批次、主体、任务组、任务 id |
| `stage` | str | 当前阶段，见 2.2 |
| `updated_at` | str | 最后写入时间（本地时间，秒级） |
| `task` | object | 任务定义 |
| `group` | object | `{id, name, variables}` |
| `prompt` | str | 渲染后的 prompt |
| `agent` | object \| null | Agent 结果，未调用前为 null |
| `pre_script` / `expect` / `clean` | object \| null | 各阶段脚本结果 |
| `result` | object \| null | 最终结论，任务结束前为 null |
| `truncated` | list[str] | 被截断的字段名（如 `agent.output`） |

`task` 字段：

```json
{
  "tid": 70001, "gid": 7, "name": "SDK 主任务", "description": "",
  "depends": [], "pre_script": "...", "expect": "...", "clean": "...",
  "timeout": null
}
```

脚本阶段结果（`pre_script` / `expect` / `clean` 同构）：

```json
{
  "returncode": 0,
  "stdout": "{\"check_result\": \"PASS\"}",
  "stderr": "",
  "success": true
}
```

完整示例：

```json
{
  "schema_version": 1,
  "batch_id": 9,
  "sid": 6,
  "gid": 7,
  "tid": 70001,
  "stage": "done",
  "updated_at": "2026-11-04T03:58:21",
  "task": {
    "tid": 70001, "gid": 7, "name": "SDK 主任务", "description": "",
    "depends": [], "pre_script": "check_scripts/sdk_pre.py",
    "expect": "check_scripts/sdk_expect.py", "clean": "check_scripts/sdk_clean.py",
    "timeout": null
  },
  "group": {
    "id": 7, "name": "sdk_dev_1793782691",
    "variables": {"DB2_SRC": {"name": "SOUR_UTF", "port": "50000"}}
  },
  "prompt": "[QA] 用 SOUR_UTF 创建数据库节点 el7",
  "agent": {
    "success": true, "output": "已完成：password 已接收，数据库节点 el7 创建成功。",
    "error": "", "returncode": 0, "duration_sec": 0.0, "rounds": 2,
    "tokens": {"input_tokens": 11, "output_tokens": 22, "total_tokens": 33},
    "usage_seen": true, "tool_calls": 1, "tool_names": ["create_db_node"],
    "first_token_ms": 2,
    "qa_record": {"requested_fields": ["password"], "asked_rounds": 1,
                  "detected_fields": ["password"], "fed_fields": ["password"]}
  },
  "pre_script": {"returncode": 0, "stdout": "...", "stderr": "", "success": true},
  "expect": {"returncode": 0, "stdout": "...", "stderr": "", "success": true},
  "clean": {"returncode": 0, "stdout": "...", "stderr": "", "success": true},
  "result": {"ok": true, "result": 1, "result_detail": 2, "verdict": "PASS",
             "agent_success": true, "expect_success": true,
             "metrics": {"metrics_version": 2}, "duration_sec": 0.1},
  "truncated": []
}
```

---

## 6. 常见用法

### 6.1 校验 Agent 是否完成了操作

```python
import ebaseline_sdk as eb

text = eb.agent_output()
ok = "创建成功" in text and "失败" not in text
eb.report(ok, detail={"matched": "创建成功" in text})
```

### 6.2 校验 QA 闭环是否发生

```python
import ebaseline_sdk as eb

record = eb.qa_record() or {}
fed = record.get("fed_fields") or []
eb.report("password" in fed, detail={"fed_fields": fed,
                                    "asked_rounds": record.get("asked_rounds", 0)})
```

### 6.3 检查 Agent 是否异常超时

```python
import ebaseline_sdk as eb

a = eb.agent()
# 校验通过但 Agent 异常（输出可能已产生）——结果页记为 PARTIAL
ok = bool(a.get("output")) and "超时" not in (a.get("error") or "")
eb.report(ok, detail={"agent_success": a.get("success"), "error": a.get("error")})
```

### 6.4 读取上游任务结果（依赖链）

```python
import argparse
import ebaseline_sdk as eb

ap = argparse.ArgumentParser()
ap.add_argument("--dep-tid", type=int, required=True)
args = ap.parse_args()

res = eb.result(args.dep_tid)
assert res.get("verdict") == "PASS", res
text = eb.agent_output(args.dep_tid)      # 上游 Agent 的完整回答
```

### 6.5 确认整条前置链路都通过

```python
import ebaseline_sdk as eb

matrix = eb.results()
failed = {tid: v["verdict"] for tid, v in matrix.items() if v["verdict"] not in ("PASS", "RUNNING")}
eb.report(not failed, detail={"failed": failed, "total": len(matrix)})
```

### 6.6 按组变量取参数

```python
import ebaseline_sdk as eb

node = eb.variable("DB2_SRC.name")
port = eb.variable("DB2_SRC.port", "50000")
```

### 6.7 兼容旧脚本（stdin）

```python
import json, sys
import ebaseline_sdk as eb

text = sys.stdin.read()          # 旧契约，仍然可用
assert text == eb.agent_output() # 两者一致
```

### 6.8 非 ebaseline 环境跑同一个脚本

```python
import ebaseline_sdk as eb

if not eb.available():
    print("无运行上下文，跳过校验")
    raise SystemExit(0)
```

---

## 7. 手工调试

在 Web 控制台的组工作空间终端里，`EBASE_GID`、`EBASE_WORKSPACE`、
`EBASE_RUNTIME_ROOT`、`PYTHONPATH` 已自动注入，SDK 会退化为读取
**该组最近一次执行**的上下文：

```bash
$ python ebaseline_sdk.py
context : /home/.../runtime/ebase_runs/group_7/batch_9/70002.json
batch   : 9  sid=6  gid=7  tid=70002
task    : SDK 下游任务
stage   : done
agent   : success=True rounds=1 tools=1
output  : 38 字符
--- preview ---
已完成：SDK 测试任务，数据库节点 el7 创建成功，状态 NORMAL。
```

查看完整快照：

```bash
python ebaseline_sdk.py --dump | python -m json.tool
```

> 手工运行的脚本不会获得 `EBASE_STAGE`，`stage()` 返回的是快照最后写入的阶段（通常是 `done`）。

---

## 8. 限制与安全

### 8.1 限制

- **只读**：SDK 不写上下文，也不更新任务状态；写结果请用 `report()` 的退出码，
  或由执行器按脚本返回码判定
- **同批次**：`context_for(tid)` 只能读同一批次内的任务，跨批次请用 REST API
- **单段文本上限**：Agent 输出超过 4,000,000 字符会被截断，
  并在 `truncated` 中标记 `agent.output`；脚本 stdout/stderr 也按同一上限落盘（不额外标记）
- **快照时机**：`pre_script` 阶段读不到 Agent 输出（此时还没调用），这是预期行为
- **保留窗口**：每组只保留最近 20 个批次目录，历史批次请以数据库记录为准

### 8.2 安全

- 上下文**包含组变量**（可能含数据库密码等），`runtime/` 应按生产数据保护，
  不要提交到版本库（已加入 `.gitignore`）
- 上下文**不包含**待测主体的 `secret` / API key
- `runtime/` 位于工作空间之外，不会通过工作空间文件接口被浏览或下载
- SDK 只读取上下文文件（以及 `qa_record()` 兼容用的 QA 记录文件），不发起网络请求

---

## 9. 故障排查

| 报错 / 现象 | 原因 | 处理 |
|-------------|------|------|
| `ContextUnavailable: 没有运行上下文：EBASE_RUN_FILE / ... 均未设置` | 脚本不是由执行器启动，也没有 `EBASE_GID` | 在工作空间终端设置 `EBASE_GID=<gid>`；或显式设置 `EBASE_RUN_FILE` |
| `ContextUnavailable: 任务组 N 还没有执行记录` | 该组从未跑过批次 | 先执行一次批次 |
| `ContextUnavailable: 上下文文件不存在: ...` | `EBASE_RUN_FILE` 指向的路径被清理或 hand-写错 | 检查 `runtime/ebase_runs/` 是否被手工删除；重跑批次 |
| `AgentOutputUnavailable: 任务 N 尚未执行到 Agent 阶段（stage=pre_script）` | 在 `pre_script` 里调用 `agent_output()` | 前置阶段只能用 `task()` / `variable()` / `prompt()` |
| `UnknownTask: 本批次没有任务 N 的上下文` | `context_for(tid)` 的 tid 不在本批次 | 确认依赖 tid 正确且属于同批次 |
| `KeyError: 组变量不存在: X` | 变量名写错且无默认值 | 用 `eb.variable("X", default)`，或检查 `eb.variables()` |
| `ModuleNotFoundError: No module named 'ebaseline_sdk'` | 手工运行且未设 `PYTHONPATH` | 执行器会自动注入；手工运行可 `export PYTHONPATH=<项目根>:$PYTHONPATH` |
| `result()` 返回 `pending=True` | 任务还在跑（当前处于 expect/agent 阶段） | 正常现象；最终结论在任务收尾后写入 |
| `qa_record()` 返回 `None` | 任务未配置 `conversation`，或 Agent 未触发索取流程 | 检查任务 `input.conversation`；用 `agent().get("rounds")` 辅助判断 |
| 脚本读到的输出是旧的 | 同一进程里读了多个任务的上下文（SDK 单条缓存按 mtime 失效） | 正常行为；每次调用都会按文件 mtime 判定是否重新加载 |

---

## 附：完整签名速查

```python
context_path() -> Path
available() -> bool

context() -> dict
context_for(tid: int) -> dict
metadata() -> dict
stage() -> str
task() -> dict
prompt() -> str
variables() -> dict
variable(path: str, default: Any = ...) -> Any

agent(tid: int | None = None) -> dict
agent_output(tid: int | None = None) -> str
qa_record(tid: int | None = None) -> dict | None
result(tid: int | None = None) -> dict
results() -> dict[int, dict]

report(ok: bool, detail: dict | None = None, *, exit: bool = True) -> bool
```
