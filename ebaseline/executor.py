"""任务执行引擎：按依赖拓扑排序执行任务，写结果到 task_result。

执行流程（每任务）：
  1. 检查依赖任务是否都已成功（result=1）
  2. 执行 pre_script（如果配置了）
  3. 渲染 input.prompt（替换 {VAR} / {VAR.SUB}）
  4. 调用 Agent API
  5. 执行 expect 校验脚本（返回码 0=成功）
  6. 执行 clean 脚本（总是执行，无论成败）
  7. 写 task_result
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from . import crud, models
from .agent_client import run_agent
from .config import get_config
from .database import _get_session_factory

# 全局线程池：批次执行异步化
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ebexec")
_running_batches: dict[int, threading.Event] = {}
_running_lock = threading.Lock()


class ExecutionError(RuntimeError):
    """任务执行失败。"""


def render_variables(text: str, variables: dict[str, Any]) -> str:
    """递归替换 {KEY} 和 {KEY.sub.key} 占位符。

    - `{{` → 字面量 `{`
    - `}}` → 字面量 `}`
    - `{KEY}` → variables[KEY]
    - `{KEY.sub}` → variables[KEY]["sub"]
    """
    if not text or not isinstance(text, str):
        return text
    if "{" not in text:
        return text

    # 转义
    text = text.replace("{{", "\x00LB\x00").replace("}}", "\x00RB\x00")

    def _lookup(path: str) -> Any:
        parts = path.split(".")
        cur: Any = variables
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                raise KeyError(path)
        return cur

    def _replace(m: re.Match) -> str:
        var_path = m.group(1)
        try:
            val = _lookup(var_path)
            if isinstance(val, (dict, list)):
                return json.dumps(val, ensure_ascii=False)
            return str(val)
        except KeyError:
            return m.group(0)  # 未找到保留原样

    text = re.sub(r"\{([^{}]+)\}", _replace, text)
    return text.replace("\x00LB\x00", "{").replace("\x00RB\x00", "}")


def _run_script(script_path: str, timeout: int = 300, extra_env: dict | None = None,
                workspace: Path | None = None) -> dict:
    """执行脚本，返回 {returncode, stdout, stderr, success}。

    script_path 可以是：
      - 绝对路径 /path/to/script.py
      - 相对路径（优先从组工作空间 workspace 解析，其次从 scripts_base_dir）
      - 命令行（含参数），用空格分隔

    脚本需要有执行权限，或通过解释器执行（根据后缀）。
    """
    cfg = get_config()
    base = cfg.scripts_base_dir

    # 解析命令行
    parts = script_path.split()
    if not parts:
        return {"returncode": -1, "stdout": "", "stderr": "空命令", "success": False}

    # 支持三种形式：
    #   1) "script.py --arg"           → 脚本 + 参数
    #   2) "python script.py ..."      → 显式解释器 + 脚本 + 参数
    #   3) "python -c 'code'"          → 内联脚本（不查文件，直接执行）
    INTERPRETERS = {"python", "python3", "bash", "sh"}
    interpreter = None
    script_file = None
    args = []
    inline_code = None

    if parts[0] in INTERPRETERS and len(parts) > 1:
        interpreter = parts[0]
        if parts[1] == "-c" and len(parts) > 2:
            # 内联代码：python -c "..."
            inline_code = " ".join(parts[2:])
            # 去掉外层引号
            if (inline_code.startswith('"') and inline_code.endswith('"')) or \
               (inline_code.startswith("'") and inline_code.endswith("'")):
                inline_code = inline_code[1:-1]
        else:
            script_file = parts[1]
            args = parts[2:]
    else:
        script_file = parts[0]
        args = parts[1:]

    # 内联代码模式：跳过文件查找
    if inline_code is not None:
        pass
    else:
        # 解析路径：组工作空间优先
        script_full = Path(script_file)
        if not script_full.is_absolute():
            ws_candidate = (workspace / script_file) if workspace else None
            if ws_candidate is not None and ws_candidate.exists():
                script_full = ws_candidate
            else:
                script_full = base / script_file
        if not script_full.exists():
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": f"脚本不存在: {script_full}",
                "success": False,
            }

    # 根据后缀选择解释器（python 脚本优先用组独立 venv 的解释器）
    suffix = script_full.suffix.lower() if inline_code is None else ""
    if workspace is not None:
        # workspace 传进来的是组工作空间目录，从中解析 venv
        from .routers.workspace import _venv_python
        ws_gid = int(workspace.name.replace("group_", ""))
        group_venv_py = _venv_python(ws_gid)
        if group_venv_py.exists():
            python_bin = str(group_venv_py)
        else:
            # 回退到项目 venv
            project_venv = Path(__file__).resolve().parent.parent / "venv" / "bin" / "python"
            python_bin = str(project_venv) if project_venv.exists() else "python3"
    else:
        project_venv = Path(__file__).resolve().parent.parent / "venv" / "bin" / "python"
        python_bin = str(project_venv) if project_venv.exists() else "python3"

    if inline_code is not None:
        # 内联代码：python -c "..."
        if interpreter in ("python", "python3"):
            cmd = [python_bin, "-c", inline_code] + args
        elif interpreter in ("bash", "sh"):
            cmd = [cfg.scripts_shell, "-c", inline_code] + args
    elif interpreter in ("python", "python3"):
        cmd = [python_bin, str(script_full)] + args
    elif interpreter in ("bash", "sh"):
        cmd = [cfg.scripts_shell, str(script_full)] + args
    elif suffix == ".py":
        cmd = [python_bin, str(script_full)] + args
    elif suffix in (".sh", ".bash"):
        cmd = [cfg.scripts_shell, str(script_full)] + args
    elif script_full.stat().st_mode & 0o111:
        cmd = [str(script_full)] + args
    else:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"脚本不可执行且无法推断解释器: {script_full}",
            "success": False,
        }

    import os
    env = os.environ.copy()
    # 组变量注入
    if extra_env:
        env.update(extra_env)
    # 工作空间信息 + venv 优先
    if workspace:
        env["EBASE_WORKSPACE"] = str(workspace)
        env["PATH"] = f"{Path(python_bin).parent}:{env.get('PATH', '')}"

    # 脚本运行目录：工作空间优先
    run_cwd = str(workspace) if workspace and workspace.exists() else str(base)

    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=run_cwd,
            env=env,
        )
        return {
            "returncode": r.returncode,
            "stdout": r.stdout,
            "stderr": r.stderr,
            "success": r.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"脚本超时 ({timeout}s)",
            "success": False,
        }
    except Exception as e:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"脚本执行异常: {e}",
            "success": False,
        }


def _parse_depends(depends_str: str | None) -> list[int]:
    if not depends_str:
        return []
    return [int(x.strip()) for x in depends_str.split(",") if x.strip()]


def _topological_sort(tasks: list[models.TaskList]) -> list[models.TaskList]:
    """按依赖拓扑排序。仅处理组内依赖；循环依赖的任务追加到末尾。"""
    by_tid = {t.tid: t for t in tasks}
    in_degree: dict[int, int] = {t.tid: 0 for t in tasks}
    graph: dict[int, list[int]] = {t.tid: [] for t in tasks}

    for t in tasks:
        for dep_tid in _parse_depends(t.depends):
            if dep_tid in by_tid:
                in_degree[t.tid] += 1
                graph[dep_tid].append(t.tid)

    queue = deque([tid for tid, deg in in_degree.items() if deg == 0])
    sorted_tids = []
    while queue:
        tid = queue.popleft()
        sorted_tids.append(tid)
        for nxt in graph[tid]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)

    # 循环依赖的任务追加到末尾
    remaining = [tid for tid in by_tid if tid not in sorted_tids]
    sorted_tids.extend(remaining)
    return [by_tid[tid] for tid in sorted_tids]


def _check_dependencies(db: Session, batch: models.TaskBatch, task: models.TaskList) -> tuple[bool, str]:
    """检查任务的所有依赖是否都已成功。"""
    deps = _parse_depends(task.depends)
    if not deps:
        return True, ""
    for dep_tid in deps:
        result = crud.get_task_result(db, batch.sid, batch.gid, dep_tid)
        if result is None:
            return False, f"依赖任务 {dep_tid} 未执行"
        if result.result != 1:
            return False, f"依赖任务 {dep_tid} 未成功 (result={result.result})"
    return True, ""


def _flatten_vars(variables: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """将嵌套 dict 拍平为 KEY_SUB=value 的环境变量形式。"""
    out: dict[str, str] = {}
    for k, v in variables.items():
        key = f"{prefix}{k}" if not prefix else f"{prefix}_{k}"
        key = key.upper()
        if isinstance(v, dict):
            out.update(_flatten_vars(v, key))
        elif isinstance(v, list):
            out[key] = json.dumps(v, ensure_ascii=False)
        else:
            out[key] = str(v)
    return out


class _TaskLogger:
    """任务执行日志器：实时写库 + 内存缓冲。

    任务开始执行时创建 result=3（执行中）的占位记录，
    每阶段结束时把累积日志 flush 到数据库，
    任务结束时更新为最终状态（1=成功 / 2=失败）。
    """

    def __init__(self, db: Session, batch: models.TaskBatch, task: models.TaskList):
        self.db = db
        self.batch = batch
        self.task = task
        self.lines: list[str] = []
        self.result_id: int | None = None
        # 创建执行中占位记录
        r = crud.create_result(db, batch.sid, batch.gid, task.tid, 3, "")
        self.result_id = r.id

    def log(self, line: str = "") -> None:
        self.lines.append(line)
        self._flush()

    def _flush(self) -> None:
        if self.result_id is not None:
            crud.update_result(self.db, self.result_id, process="\n".join(self.lines))

    def finish(self, result: int) -> None:
        """任务结束：更新最终状态。"""
        if self.result_id is not None:
            crud.update_result(self.db, self.result_id, result=result, process="\n".join(self.lines))


def _execute_single_task(
    db: Session,
    batch: models.TaskBatch,
    subject: models.Subject,
    group: models.TaskGroup,
    task: models.TaskList,
    cancel_event: threading.Event,
) -> dict:
    """执行单个任务，返回执行详情。"""
    cfg = get_config()
    start_time = time.time()

    # 创建实时日志器（立即写入 result=3 占位记录）
    logger = _TaskLogger(db, batch, task)
    logger.log(f"=== 任务 {task.tid} ({task.name}) 开始执行 ===")
    logger.log(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. 检查依赖
    logger.log("\n--- 阶段 1: 检查依赖 ---")
    ok, reason = _check_dependencies(db, batch, task)
    logger.log(f"依赖检查: {'通过' if ok else '失败: ' + reason}")
    if not ok:
        logger.finish(2)
        return {
            "success": False,
            "result_detail": 0,
            "stage": "dependency",
            "reason": reason,
            "log": "\n".join(logger.lines),
            "duration_sec": round(time.time() - start_time, 1),
        }

    # 解析组变量（前置/Agent/校验/清理阶段都可能用到）
    group_vars: dict = {}
    if group.variables:
        try:
            group_vars = json.loads(group.variables)
        except json.JSONDecodeError:
            pass
    # 将组变量拍平为环境变量注入脚本子进程（如 DB2_NODE_SRC_IP=192.168.34.8）
    script_env = _flatten_vars(group_vars) if group_vars else None

    # 组工作空间目录（脚本优先从这里解析，cwd 也是这里）
    from .routers.workspace import _workspace_dir
    workspace = _workspace_dir(batch.gid)

    # 2. 执行前置脚本
    pre_result = None
    if task.pre_script:
        logger.log(f"\n--- 阶段 2: 前置脚本 ---\n命令: {task.pre_script}\n工作目录: {workspace}")
        pre_result = _run_script(task.pre_script, timeout=cfg.agent_default_timeout,
                                 extra_env=script_env, workspace=workspace)
        logger.log(f"返回码: {pre_result['returncode']}")
        if pre_result["stdout"]:
            logger.log(f"stdout: {pre_result['stdout'][:2000]}")
        if pre_result["stderr"]:
            logger.log(f"stderr: {pre_result['stderr'][:2000]}")
        if not pre_result["success"]:
            # 前置失败也要执行 clean
            if task.clean:
                logger.log("\n--- 阶段 C: 清理脚本（前置失败后执行）---")
                clean_result = _run_script(task.clean, extra_env=script_env, workspace=workspace)
                logger.log(f"返回码: {clean_result['returncode']}")
            logger.finish(2)
            return {
                "success": False,
                "result_detail": 0,
                "stage": "pre_script",
                "reason": f"前置脚本失败 (returncode={pre_result['returncode']})",
                "log": "\n".join(logger.lines),
                "duration_sec": round(time.time() - start_time, 1),
            }

    # 3. 渲染 prompt
    logger.log("\n--- 阶段 3: 渲染 prompt ---")
    input_data: dict = {}
    if task.input:
        try:
            input_data = json.loads(task.input)
        except json.JSONDecodeError as e:
            logger.finish(2)
            return {
                "success": False,
                "result_detail": 0,
                "stage": "parse_input",
                "reason": f"input JSON 解析失败: {e}",
                "log": "\n".join(logger.lines),
                "duration_sec": round(time.time() - start_time, 1),
            }

    prompt_template = input_data.get("prompt", "")
    prompt = render_variables(prompt_template, group_vars)
    logger.log(f"prompt: {prompt[:500]}{'...' if len(prompt) > 500 else ''}")

    conversation = input_data.get("conversation", [])
    qa_missing = None
    if conversation:
        # 转换为 agent_client 的 qa_missing 格式
        qa_missing = [
            {"field": c.get("field", ""), "value": c.get("answer", "")}
            for c in conversation if isinstance(c, dict)
        ]

    # 任务级超时覆盖：input.timeout > config.agent.default_timeout
    task_timeout = input_data.get("timeout")
    if task_timeout is not None:
        try:
            task_timeout = int(task_timeout)
        except (TypeError, ValueError):
            task_timeout = None
    agent_timeout = task_timeout if task_timeout else cfg.agent_default_timeout

    # 4. 调用 Agent
    logger.log(f"\n--- 阶段 4: 调用 Agent ---\n目标: {subject.ipaddr}:{subject.port}\n超时: {agent_timeout}s")
    agent_base_url = f"http://{subject.ipaddr}:{subject.port}"
    agent_result = run_agent(
        base_url=agent_base_url,
        api_key=subject.secret,
        input_text=prompt,
        timeout=agent_timeout,
        qa_missing=qa_missing,
    )
    logger.log(f"Agent 调用: {'成功' if agent_result.success else '失败'}")
    logger.log(f"耗时: {agent_result.duration_sec}s, 轮次: {agent_result.rounds}")
    if agent_result.tokens:
        logger.log(f"tokens: {agent_result.tokens}")
    if agent_result.error:
        logger.log(f"错误: {agent_result.error}")
    logger.log(f"输出: {agent_result.output[:2000]}{'...' if len(agent_result.output) > 2000 else ''}")

    agent_success = agent_result.success

    # 5. 执行 expect 校验脚本
    expect_results: list[dict] = []
    expect_success = True
    if task.expect:
        logger.log(f"\n--- 阶段 5: 期望校验 ---\n命令: {task.expect}")
        expect_result = _run_script(task.expect, timeout=cfg.agent_default_timeout,
                                    extra_env=script_env, workspace=workspace)
        expect_results.append(expect_result)
        logger.log(f"返回码: {expect_result['returncode']}")
        if expect_result["stdout"]:
            logger.log(f"stdout: {expect_result['stdout'][:2000]}")
        if expect_result["stderr"]:
            logger.log(f"stderr: {expect_result['stderr'][:2000]}")
        expect_success = expect_result["success"]
        logger.log(f"校验结果: {'通过' if expect_success else '失败'}")

    # 6. 执行 clean 脚本（总是执行）
    clean_result = None
    if task.clean:
        logger.log(f"\n--- 阶段 6: 清理脚本 ---\n命令: {task.clean}")
        clean_result = _run_script(task.clean, extra_env=script_env, workspace=workspace)
        logger.log(f"返回码: {clean_result['returncode']}")

    overall_success = agent_success and expect_success

    # 细分结果状态（供 UI 区分显示）：
    #   2 = 完全成功（Agent + 校验都通过）
    #   1 = 校验成功但 Agent 调用失败（Agent 可能已干完活但响应超时/断开）
    #   0 = 完全失败
    if overall_success:
        result_detail = 2
    elif expect_success and not agent_success:
        result_detail = 1
    else:
        result_detail = 0

    status_text = {2: "成功", 1: "校验通过(Agent异常)", 0: "失败"}[result_detail]
    logger.log(f"\n=== 任务 {task.tid} {status_text} (耗时 {round(time.time() - start_time, 1)}s) ===")

    # 更新最终状态（1=成功 2=失败）
    logger.finish(1 if result_detail == 2 else 2)

    return {
        "success": overall_success,
        "result_detail": result_detail,
        "agent_success": agent_success,
        "expect_success": expect_success,
        "agent_result": {
            "output": agent_result.output[:5000],
            "error": agent_result.error,
            "duration_sec": agent_result.duration_sec,
            "rounds": agent_result.rounds,
            "tokens": agent_result.tokens,
        },
        "pre_result": pre_result,
        "expect_results": expect_results,
        "clean_result": clean_result,
        "log": "\n".join(logger.lines),
        "duration_sec": round(time.time() - start_time, 1),
    }


def _run_batch_sync(batch_id: int, cancel_event: threading.Event) -> None:
    """同步执行批次（在线程池中运行）。"""
    db = _get_session_factory()()
    try:
        batch = crud.get_batch(db, batch_id)
        if batch is None:
            return
        subject = crud.get_subject(db, batch.sid)
        group = crud.get_task_group(db, batch.gid)
        if subject is None or group is None:
            crud.update_batch_status(db, batch_id, 3)
            return

        crud.update_batch_status(db, batch_id, 1)  # 执行中

        tasks = crud.list_tasks(db, gid=batch.gid)
        sorted_tasks = _topological_sort(tasks)

        # 清除该批次的旧结果（重跑场景）
        crud.delete_results_by_batch(db, batch.sid, batch.gid)

        total = len(sorted_tasks)
        success_count = 0
        failed_count = 0
        skipped_count = 0

        for task in sorted_tasks:
            if cancel_event.is_set():
                break
            try:
                detail = _execute_single_task(db, batch, subject, group, task, cancel_event)
            except Exception as e:
                import traceback
                err_log = f"执行异常: {e}\n" + traceback.format_exc()
                # 异常场景也写一条失败记录
                crud.create_result(db, sid=batch.sid, gid=batch.gid, tid=task.tid,
                                   result=2, process=err_log)
                detail = {"success": False, "result_detail": 0}

            if detail.get("result_detail") == 2:
                success_count += 1
            else:
                failed_count += 1
                # 失败任务的下游任务被跳过（因为依赖检查会失败）

        # 更新批次状态
        if failed_count == 0 and success_count == total:
            final_status = 2  # 已完成
        elif success_count > 0:
            final_status = 4  # 部分成功
        else:
            final_status = 3  # 执行失败
        crud.update_batch_status(db, batch_id, final_status)
    finally:
        db.close()
        with _running_lock:
            _running_batches.pop(batch_id, None)


def start_batch_async(batch_id: int) -> bool:
    """异步启动批次。返回是否成功启动（已在运行则返回 False）。"""
    with _running_lock:
        if batch_id in _running_batches:
            return False
        cancel_event = threading.Event()
        _running_batches[batch_id] = cancel_event
    _executor.submit(_run_batch_sync, batch_id, cancel_event)
    return True


def cancel_batch(batch_id: int) -> bool:
    """取消正在执行的批次。"""
    with _running_lock:
        event = _running_batches.get(batch_id)
        if event is None:
            return False
        event.set()
        return True


def is_batch_running(batch_id: int) -> bool:
    with _running_lock:
        return batch_id in _running_batches
