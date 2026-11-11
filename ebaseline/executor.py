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
import logging
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional,  Any

from sqlalchemy.orm import Session

from . import crud, manifest as manifest_mod, models, runtime, task_graph
from .agent_client import run_agent
from .config import get_config
from .database import _get_session_factory
from .metrics import result_verdict
from .security import collect_secret_values, redact_text

logger = logging.getLogger(__name__)

# 全局线程池：批次执行异步化
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ebexec")
_running_batches: dict[int, threading.Event] = {}
# 批次内单任务执行：(batch_id, tid) -> 取消事件
_running_tasks: dict[tuple[int, int], threading.Event] = {}
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


def _terminate_process_group(proc: subprocess.Popen, grace: float = 3.0) -> None:
    """先 TERM 整个进程组，超时后 KILL，避免脚本子进程残留。"""
    import os
    import signal

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def _run_script(script_path: str, timeout: int = 300, extra_env: Optional[dict] = None,
                workspace: Optional[Path] = None, stdin_text: Optional[str] = None,
                cancel_event: Optional[threading.Event] = None) -> dict:
    """执行脚本，返回 {returncode, stdout, stderr, success}。

    script_path 可以是：
      - 绝对路径 /path/to/script.py
      - 相对路径（优先从组工作空间 workspace 解析，其次从 scripts_base_dir）
      - 命令行（含参数），用空格分隔

    stdin_text 不为 None 时写入子进程 stdin（非交互脚本可据此读取输入）。

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
    # 项目根加入 PYTHONPATH：脚本可直接 import ebaseline_sdk
    project_root = str(Path(__file__).resolve().parent.parent)
    existing_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = project_root + (os.pathsep + existing_path if existing_path else "")
    env["EBASE_RUNTIME_ROOT"] = str(runtime.RUNTIME_ROOT)
    # 组变量注入
    if extra_env:
        env.update(extra_env)
    # 工作空间信息 + venv 优先
    if workspace:
        env["EBASE_WORKSPACE"] = str(workspace)
        env["PATH"] = f"{Path(python_bin).parent}:{env.get('PATH', '')}"

    # 脚本运行目录：工作空间优先；配置的 scripts 目录不存在时回退到项目根，
    # 避免内联命令（如 bash -c）因为 cwd 不存在而启动失败
    if workspace and workspace.exists():
        run_cwd = str(workspace)
    elif base.exists():
        run_cwd = str(base)
    else:
        run_cwd = str(Path(__file__).resolve().parent.parent)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            text=True,
            cwd=run_cwd,
            env=env,
            start_new_session=True,  # 独立进程组：取消/超时可整组回收
        )
    except Exception as e:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"脚本启动异常: {e}",
            "success": False,
        }

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    def _drain(pipe, sink: list[str]) -> None:
        try:
            for chunk in iter(pipe.readline, ""):
                sink.append(chunk)
        except (OSError, ValueError):
            pass
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    reader_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_parts), daemon=True)
    reader_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_parts), daemon=True)
    reader_out.start()
    reader_err.start()

    if stdin_text is not None and proc.stdin is not None:
        def _feed_stdin() -> None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        threading.Thread(target=_feed_stdin, daemon=True).start()

    deadline = time.monotonic() + timeout
    cancelled = False
    timed_out = False
    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            _terminate_process_group(proc)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _terminate_process_group(proc)
            break
        time.sleep(0.1)

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
    reader_out.join(timeout=2)
    reader_err.join(timeout=2)

    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)
    if cancelled:
        stderr = (stderr + "\n" if stderr else "") + "已取消"
        return {
            "returncode": -2,
            "stdout": stdout,
            "stderr": stderr,
            "success": False,
            "cancelled": True,
        }
    if timed_out:
        stderr = (stderr + "\n" if stderr else "") + f"脚本超时 ({timeout}s)"
        return {
            "returncode": -1,
            "stdout": stdout,
            "stderr": stderr,
            "success": False,
            "timed_out": True,
        }
    return {
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "success": proc.returncode == 0,
        "cancelled": False,
    }


def _parse_depends(depends_str: Optional[str]) -> list[int]:
    return task_graph.parse_depends(depends_str)


def _topological_sort(tasks: list[models.TaskList]) -> list[models.TaskList]:
    """按依赖拓扑排序；缺失/跨组/自依赖/循环依赖直接抛 DependencyError。"""
    return task_graph.topological_sort(tasks)


def _check_dependencies(db: Session, batch: models.TaskBatch, task: models.TaskList,
                        attempt: Optional[int] = None) -> tuple[bool, str]:
    """检查任务的所有依赖是否都已成功。

    attempt>0 时只认本轮结果，避免上一轮的旧结果让下游误以为依赖已满足；
    attempt=0/None 兼容旧数据（旧结果没有 attempt 字段）。
    """
    deps = _parse_depends(task.depends)
    if not deps:
        return True, ""
    for dep_tid in deps:
        result = crud.get_batch_task_result(
            db, batch.id, dep_tid, attempt=attempt if attempt else None)
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


def _base_metrics(start_time: float, **extra: Any) -> dict:
    """任务结束时写入 task_result.metrics 的基础字段。"""
    data: dict[str, Any] = {
        "metrics_version": 2,
        "duration_sec": round(time.time() - start_time, 1),
    }
    data.update(extra)
    return data


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class _RunContext:
    """任务运行上下文快照。

    每个阶段把最新状态原子写入 runtime/ebase_runs/group_<gid>/batch_<bid>/<tid>.json，
    工作空间脚本通过 ebaseline_sdk 读取，不必依赖 stdin 或自行查库。
    下游任务读取上游输出时，直接读同目录的 <tid>.json。
    """

    def __init__(self, batch: models.TaskBatch, group: models.TaskGroup,
                 task: models.TaskList, attempt: Optional[int] = None,
                 variables: Optional[dict] = None):
        self.attempt = attempt if attempt is not None else getattr(batch, "attempt", None)
        self.path = runtime.task_context_path(batch.gid, batch.id, task.tid, self.attempt)
        self.run_dir = runtime.batch_dir(batch.gid, batch.id, self.attempt)
        self.qa_path = runtime.qa_record_path(batch.gid, batch.id, task.tid, self.attempt)
        self.batch_id = batch.id
        self.sid = batch.sid
        self.gid = batch.gid
        self.tid = task.tid

        self.group_vars: dict = {}
        if variables is not None:
            self.group_vars = variables
        elif group.variables:
            try:
                self.group_vars = json.loads(group.variables)
            except json.JSONDecodeError:
                self.group_vars = {}

        self.data: dict[str, Any] = {
            "schema_version": runtime.SCHEMA_VERSION,
            "batch_id": batch.id,
            "attempt": self.attempt,
            "sid": batch.sid,
            "gid": batch.gid,
            "tid": task.tid,
            "stage": "start",
            "updated_at": _now_iso(),
            "task": {
                "tid": task.tid,
                "gid": task.gid,
                "name": task.name,
                "description": task.description,
                "depends": _parse_depends(task.depends),
                "pre_script": task.pre_script,
                "expect": task.expect,
                "clean": task.clean,
                "timeout": task.timeout,
            },
            "group": {"id": group.id, "name": group.name, "variables": self.group_vars},
            "prompt": "",
            "agent": None,
            "pre_script": None,
            "expect": None,
            "clean": None,
            "result": None,
            "truncated": [],
        }
        self._flush("start")

    def _flush(self, stage: str, **sections: Any) -> None:
        self.data["stage"] = stage
        self.data["updated_at"] = _now_iso()
        self.data.update(sections)
        try:
            runtime.write_context(self.path, self.data)
        except OSError:
            # 上下文写失败不应中断任务执行（脚本会退化为读 stdin）
            pass

    def script_env(self, stage: str) -> dict[str, str]:
        """脚本阶段的 EBASE_* 环境变量。"""
        env = {
            "EBASE_RUN_FILE": str(self.path),
            "EBASE_RUN_DIR": str(self.run_dir),
            "EBASE_BATCH_ID": str(self.batch_id),
            "EBASE_SID": str(self.sid),
            "EBASE_GID": str(self.gid),
            "EBASE_TID": str(self.tid),
            "EBASE_STAGE": stage,
        }
        if self.qa_path.exists():
            env["EBASE_QA_FILE"] = str(self.qa_path)
            env["I2STREAM_AGENT_QA_FILE"] = str(self.qa_path)  # 兼容旧校验脚本
        return env

    def set_prompt(self, prompt: str) -> None:
        self._flush("render", prompt=prompt)

    def set_agent(self, agent_result) -> None:
        output, truncated = runtime.cap_text(agent_result.output)
        self.data["agent"] = {
            "success": agent_result.success,
            "output": output,
            "error": agent_result.error,
            "returncode": agent_result.returncode,
            "duration_sec": agent_result.duration_sec,
            "rounds": agent_result.rounds,
            "tokens": agent_result.tokens,
            "usage_seen": agent_result.usage_seen,
            "tool_calls": agent_result.tool_calls,
            "tool_names": agent_result.tool_names,
            "first_token_ms": agent_result.first_token_ms,
            "qa_record": agent_result.qa_record,
        }
        if truncated:
            self.data["truncated"].append("agent.output")
        self._flush("agent")
        if agent_result.qa_record:
            try:
                runtime.write_json(self.qa_path, agent_result.qa_record)
            except OSError:
                pass

    def enter(self, stage: str) -> None:
        """进入某阶段（脚本启动前调用，使 stage() 反映正在运行的阶段）。"""
        self._flush(stage)

    def set_script_result(self, name: str, result: Optional[dict], stage: str) -> None:
        """记录某阶段脚本结果（pre_script/expect/clean）。"""
        if result is None:
            self._flush(stage)
            return
        section = {
            "returncode": result.get("returncode"),
            "stdout": runtime.cap_text(result.get("stdout") or "")[0],
            "stderr": runtime.cap_text(result.get("stderr") or "")[0],
            "success": result.get("success"),
        }
        self._flush(stage, **{name: section})

    def finish(self, result: dict) -> None:
        self._flush("done", result=result)


class _TaskLogger:
    """任务执行日志器：实时写库 + 内存缓冲。

    任务开始执行时创建 result=3（执行中）的占位记录，
    每阶段结束时把累积日志 flush 到数据库，
    任务结束时更新为最终状态（1=成功 / 2=失败）并写入结构化指标。
    """

    def __init__(self, db: Session, batch: models.TaskBatch, task: models.TaskList,
                 attempt: Optional[int] = None, secrets: Optional[set[str]] = None):
        self.db = db
        self.batch = batch
        self.task = task
        self.attempt = attempt if attempt is not None else getattr(batch, "attempt", None)
        self.secrets = secrets or set()
        self.placeholder = get_config().security_mask_placeholder
        self.lines: list[str] = []
        self.result_id: Optional[int] = None
        # 创建执行中占位记录
        r = crud.create_result(
            db, batch.sid, batch.gid, task.tid, 3, "",
            batch_id=batch.id,
            attempt=self.attempt,
            metrics=json.dumps({"metrics_version": 2, "stage": "running"}, ensure_ascii=False),
        )
        self.result_id = r.id

    def log(self, line: str = "") -> None:
        self.lines.append(redact_text(line, self.secrets, self.placeholder))
        self._flush()

    def _flush(self) -> None:
        if self.result_id is not None:
            crud.update_result(self.db, self.result_id, process="\n".join(self.lines))

    def finish(
        self,
        result: int,
        result_detail: Optional[int] = None,
        metrics: Optional[dict] = None,
    ) -> None:
        """任务结束：更新最终状态、细分状态与结构化指标。"""
        if self.result_id is not None:
            crud.update_result(
                self.db,
                self.result_id,
                result=result,
                result_detail=result_detail,
                metrics=json.dumps(metrics, ensure_ascii=False) if metrics is not None else None,
                process="\n".join(self.lines),
            )


def _execute_single_task(
    db: Session,
    batch: models.TaskBatch,
    subject: models.Subject,
    group: models.TaskGroup,
    task: models.TaskList,
    cancel_event: threading.Event,
    ignore_dependencies: bool = False,
    group_vars_override: Optional[dict] = None,
    definition_source: str = "live",
    definition_mismatch: bool = False,
) -> dict:
    """执行单个任务，返回执行详情。

    ignore_dependencies=True 时跳过依赖检查（批次详情页单独执行任务用）。
    group_vars_override: 整批执行时传入 manifest 冻结的组变量；
    definition_source: live=当前任务定义，manifest=批次冻结定义。
    """
    cfg = get_config()
    start_time = time.time()
    max_script_timeout = cfg.agent_default_timeout

    # 组变量：manifest 冻结值优先（整批重跑必须与批次创建时一致）
    group_vars: dict = {}
    if group_vars_override is not None:
        group_vars = group_vars_override
    elif group.variables:
        try:
            parsed = json.loads(group.variables)
            group_vars = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            group_vars = {}
    secrets = collect_secret_values(group_vars)
    expect_configured = bool((task.expect or "").strip())

    # 创建实时日志器（立即写入 result=3 占位记录）
    logger = _TaskLogger(
        db, batch, task, attempt=getattr(batch, "attempt", None), secrets=secrets)
    logger.log(f"=== 任务 {task.tid} ({task.name}) 开始执行 ===")
    logger.log(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    if definition_source == "manifest":
        logger.log("任务定义: 批次创建时冻结的 manifest")
    elif definition_mismatch:
        logger.log("任务定义: 当前定义（与批次 manifest 不一致）")

    # 运行上下文：每阶段落盘，供工作空间脚本通过 ebaseline_sdk 读取
    ctx = _RunContext(
        batch, group, task, attempt=getattr(batch, "attempt", None),
        variables=group_vars)

    # 将组变量拍平为环境变量注入脚本子进程（如 DB2_NODE_SRC_IP=192.168.34.8）
    script_env = _flatten_vars(group_vars) if group_vars else None

    # 组工作空间目录（脚本优先从这里解析，cwd 也是这里）
    from .routers.workspace import _workspace_dir
    workspace = _workspace_dir(batch.gid)

    def stage_env(stage: str) -> Optional[dict]:
        """组变量 + 运行上下文环境变量。"""
        env = dict(script_env or {})
        env.update(ctx.script_env(stage))
        return env

    clean_state: dict = {"done": False, "result": None}

    def run_clean() -> Optional[dict]:
        """执行清理脚本，保证最多执行一次；取消后仍执行（不继承取消信号）。"""
        if clean_state["done"] or not task.clean:
            return clean_state["result"]
        clean_state["done"] = True
        logger.log(f"\n--- 阶段 6: 清理脚本 ---\n命令: {task.clean}")
        ctx.enter("clean")
        result = _run_script(
            task.clean, timeout=max_script_timeout, extra_env=stage_env("clean"),
            workspace=workspace, cancel_event=None)
        logger.log(f"返回码: {result['returncode']}")
        if result.get("stdout"):
            logger.log(f"stdout: {result['stdout'][:2000]}")
        if result.get("stderr"):
            logger.log(f"stderr: {result['stderr'][:2000]}")
        ctx.set_script_result("clean", result, "clean")
        clean_state["result"] = result
        return result

    def finish(result: int, detail: Optional[int], metrics: dict,
               stage: str, reason: str = "") -> dict:
        """统一收尾：执行 clean、写入结果/上下文并返回执行详情。"""
        clean_result = run_clean()
        if clean_result is None:
            clean_success: Optional[bool] = True
        elif clean_result.get("cancelled"):
            clean_success = None
            metrics["clean_cancelled"] = True
        else:
            clean_success = bool(clean_result.get("success"))
        metrics["clean_success"] = clean_success
        metrics["clean_returncode"] = (
            clean_result.get("returncode") if clean_result else None)

        # 清理失败不能被 PASS/UNVALIDATED/PARTIAL 掩盖；已经是 FAIL/SKIP 的
        # 任务保留原阶段和原因，clean 失败仍通过 clean_success 指标记录。
        if clean_success is False and (result == 1 or detail in (1, 2, 3)):
            result, detail, stage = 2, 0, "clean"
            reason = reason or "清理脚本失败"

        metrics["stage"] = stage
        metrics.setdefault("expect_configured", expect_configured)
        metrics["definition_source"] = definition_source
        if definition_mismatch:
            metrics["definition_mismatch"] = True
        verdict = result_verdict(result, detail, metrics)

        detail_text = {2: "成功", 3: "未校验", 1: "校验通过(Agent异常)", 0: "失败"}
        if detail in detail_text:
            status_text = detail_text[detail]
        else:
            status_text = "跳过" if stage in ("dependency", "pre_script", "cancelled") else "失败"
        logger.log(
            f"\n=== 任务 {task.tid} {status_text} "
            f"(耗时 {round(time.time() - start_time, 1)}s) ===")

        logger.finish(result, result_detail=detail, metrics=metrics)
        ctx_payload = {
            "ok": verdict in ("PASS", "UNVALIDATED"),
            "success": result == 1,
            "result": result,
            "result_detail": detail,
            "verdict": verdict,
            "stage": stage,
            "reason": reason,
            "agent_success": metrics.get("agent_success"),
            "expect_configured": metrics.get("expect_configured"),
            "expect_success": metrics.get("expect_success"),
            "validated": metrics.get("validated"),
            "clean_success": clean_success,
            "metrics": metrics,
            "duration_sec": metrics["duration_sec"],
        }
        ctx.finish(ctx_payload)
        return {
            "success": result == 1,
            "result": result,
            "result_detail": detail,
            "verdict": verdict,
            "stage": stage,
            "reason": reason,
            "agent_success": metrics.get("agent_success"),
            "expect_configured": metrics.get("expect_configured"),
            "expect_success": metrics.get("expect_success"),
            "validated": metrics.get("validated"),
            "clean_success": clean_success,
            "metrics": metrics,
            "agent_result": state.get("agent_result"),
            "pre_result": state.get("pre_result"),
            "expect_results": state.get("expect_results") or [],
            "clean_result": clean_result,
            "log": "\n".join(logger.lines),
            "duration_sec": round(time.time() - start_time, 1),
        }

    state: dict = {}

    try:
        # 1. 检查依赖
        logger.log("\n--- 阶段 1: 检查依赖 ---")
        if ignore_dependencies:
            ok, reason = True, ""
            logger.log("依赖检查: 已跳过（单独执行，忽略依赖）")
        else:
            ok, reason = _check_dependencies(
                db, batch, task, attempt=getattr(batch, "attempt", 0))
            logger.log(f"依赖检查: {'通过' if ok else '失败: ' + reason}")
        if not ok:
            metrics = _base_metrics(start_time, stage="dependency", reason=reason)
            return finish(2, None, metrics, "dependency", reason)

        if cancel_event.is_set():
            metrics = _base_metrics(start_time, stage="cancelled", reason="用户取消")
            return finish(2, None, metrics, "cancelled", "用户取消")

        # 2. 执行前置脚本
        pre_result = None
        if task.pre_script:
            logger.log(f"\n--- 阶段 2: 前置脚本 ---\n命令: {task.pre_script}\n工作目录: {workspace}")
            ctx.enter("pre_script")
            pre_result = _run_script(
                task.pre_script, timeout=max_script_timeout,
                extra_env=stage_env("pre_script"), workspace=workspace,
                cancel_event=cancel_event)
            state["pre_result"] = pre_result
            logger.log(f"返回码: {pre_result['returncode']}")
            if pre_result["stdout"]:
                logger.log(f"stdout: {pre_result['stdout'][:2000]}")
            if pre_result["stderr"]:
                logger.log(f"stderr: {pre_result['stderr'][:2000]}")
            ctx.set_script_result("pre_script", pre_result, "pre_script")
            if pre_result.get("cancelled"):
                metrics = _base_metrics(
                    start_time, stage="cancelled", reason="用户取消",
                    pre_script_returncode=pre_result["returncode"])
                return finish(2, None, metrics, "cancelled", "用户取消")
            if not pre_result["success"]:
                metrics = _base_metrics(
                    start_time,
                    stage="pre_script",
                    pre_script_returncode=pre_result["returncode"],
                )
                reason = f"前置脚本失败 (returncode={pre_result['returncode']})"
                return finish(2, None, metrics, "pre_script", reason)

        # 3. 渲染 prompt
        logger.log("\n--- 阶段 3: 渲染 prompt ---")
        input_data: dict = {}
        if task.input:
            try:
                input_data = json.loads(task.input)
            except json.JSONDecodeError as e:
                metrics = _base_metrics(
                    start_time, stage="parse_input", reason=str(e))
                return finish(2, 0, metrics, "parse_input", f"input JSON 解析失败: {e}")

        prompt_template = input_data.get("prompt", "")
        prompt = render_variables(prompt_template, group_vars)
        logger.log(f"prompt: {prompt[:500]}{'...' if len(prompt) > 500 else ''}")
        ctx.set_prompt(prompt)

        conversation = input_data.get("conversation", [])
        qa_missing = None
        if conversation:
            # 转换为 agent_client 的 qa_missing 格式
            qa_missing = [
                {"field": c.get("field", ""), "value": c.get("answer", "")}
                for c in conversation if isinstance(c, dict)
            ]

        # 任务级超时：task.timeout > input.timeout（旧字段，兼容） > config.agent.default_timeout
        agent_timeout = task.timeout
        timeout_source = "任务设置"
        if not agent_timeout or agent_timeout <= 0:
            legacy_timeout = input_data.get("timeout")
            if legacy_timeout is not None:
                try:
                    legacy_timeout = int(legacy_timeout)
                except (TypeError, ValueError):
                    legacy_timeout = None
            if legacy_timeout and legacy_timeout > 0:
                agent_timeout = legacy_timeout
                timeout_source = "input.timeout（旧字段）"
            else:
                agent_timeout = cfg.agent_default_timeout
                timeout_source = "全局默认"

        if cancel_event.is_set():
            metrics = _base_metrics(start_time, stage="cancelled", reason="用户取消")
            return finish(2, None, metrics, "cancelled", "用户取消")

        # 4. 调用 Agent
        logger.log(
            f"\n--- 阶段 4: 调用 Agent ---\n目标: {subject.ipaddr}:{subject.port}\n"
            f"超时: {agent_timeout}s（{timeout_source}）")
        agent_base_url = f"http://{subject.ipaddr}:{subject.port}"
        agent_result = run_agent(
            base_url=agent_base_url,
            api_key=subject.secret,
            input_text=prompt,
            timeout=agent_timeout,
            qa_missing=qa_missing,
            cancel_event=cancel_event,
        )
        state["agent_result"] = {
            "output": agent_result.output[:5000],
            "error": agent_result.error,
            "duration_sec": agent_result.duration_sec,
            "rounds": agent_result.rounds,
            "tokens": agent_result.tokens,
            "usage_seen": agent_result.usage_seen,
            "tool_calls": agent_result.tool_calls,
            "tool_names": agent_result.tool_names,
            "first_token_ms": agent_result.first_token_ms,
            "cancelled": agent_result.cancelled,
        }
        logger.log(f"Agent 调用: {'成功' if agent_result.success else '失败'}")
        logger.log(f"耗时: {agent_result.duration_sec}s, 轮次: {agent_result.rounds}, "
                   f"工具调用: {agent_result.tool_calls} 次")
        if agent_result.first_token_ms is not None:
            logger.log(f"首字延迟: {agent_result.first_token_ms}ms")
        if agent_result.usage_seen:
            logger.log(f"tokens: {agent_result.tokens}")
        else:
            logger.log("tokens: 缺失")
        if agent_result.error:
            logger.log(f"错误: {agent_result.error}")
        logger.log(
            f"输出: {agent_result.output[:2000]}"
            f"{'...' if len(agent_result.output) > 2000 else ''}")

        # 落盘完整输出 + qa_record，供 expect/clean/下游任务脚本通过 SDK 读取
        ctx.set_agent(agent_result)
        agent_success = bool(agent_result.success)

        if agent_result.cancelled or cancel_event.is_set():
            metrics = _base_metrics(
                start_time,
                stage="cancelled",
                reason="用户取消",
                agent_duration_sec=agent_result.duration_sec,
                rounds=agent_result.rounds,
                tool_calls=agent_result.tool_calls,
                agent_success=agent_success,
                cancelled=True,
            )
            return finish(2, None, metrics, "cancelled", "用户取消")

        # 5. 执行 expect 校验脚本
        expect_results: list[dict] = []
        expect_success: Optional[bool] = None
        if expect_configured:
            logger.log(f"\n--- 阶段 5: 期望校验 ---\n命令: {task.expect}")
            logger.log(f"通过 stdin 传入 Agent 回答（{len(agent_result.output)} 字符）")
            ctx.enter("expect")
            expect_result = _run_script(
                task.expect, timeout=max_script_timeout,
                extra_env=stage_env("expect"), workspace=workspace,
                stdin_text=agent_result.output, cancel_event=cancel_event)
            expect_results.append(expect_result)
            state["expect_results"] = expect_results
            logger.log(f"返回码: {expect_result['returncode']}")
            if expect_result["stdout"]:
                logger.log(f"stdout: {expect_result['stdout'][:2000]}")
            if expect_result["stderr"]:
                logger.log(f"stderr: {expect_result['stderr'][:2000]}")
            ctx.set_script_result("expect", expect_result, "expect")
            if expect_result.get("cancelled"):
                metrics = _base_metrics(
                    start_time, stage="cancelled", reason="用户取消",
                    agent_success=agent_success,
                    expect_configured=True,
                    expect_returncode=expect_result["returncode"])
                return finish(2, None, metrics, "cancelled", "用户取消")
            expect_success = bool(expect_result["success"])
            logger.log(f"校验结果: {'通过' if expect_success else '失败'}")

        # 6. 计算质量判定（clean 在外层 finish 中执行并可能覆盖）
        validated = bool(expect_configured and expect_success)
        if agent_success and (not expect_configured or expect_success):
            result = 1
            detail = 2 if expect_configured else 3
        elif expect_configured and expect_success and not agent_success:
            # 校验通过但 Agent 异常：保留证据，但下游依赖仍会阻断
            result, detail = 2, 1
        else:
            result, detail = 2, 0

        if detail == 2:
            stage = "ok"
        elif detail == 3:
            stage = "unvalidated"
        elif not agent_success:
            stage = "agent"
        else:
            stage = "expect"

        # 结构化指标：token 缺失时写 null，不按 0 计入均值
        tokens = None
        if agent_result.usage_seen:
            tokens = {
                "input": agent_result.tokens.get("input_tokens"),
                "output": agent_result.tokens.get("output_tokens"),
                "total": agent_result.tokens.get("total_tokens"),
            }
            if not tokens.get("total"):
                tokens["total"] = (tokens.get("input") or 0) + (tokens.get("output") or 0)

        task_metrics = _base_metrics(
            start_time,
            stage=stage,
            agent_duration_sec=agent_result.duration_sec,
            rounds=agent_result.rounds,
            tool_calls=agent_result.tool_calls,
            tool_names=agent_result.tool_names,
            first_token_ms=agent_result.first_token_ms,
            tokens=tokens,
            usage_seen=agent_result.usage_seen,
            agent_success=agent_success,
            expect_configured=expect_configured,
            expect_success=expect_success,
            validated=validated,
            pre_script_ok=pre_result is None or pre_result.get("success"),
            expect_returncode=(expect_results[0]["returncode"] if expect_results else None),
        )
        return finish(result, detail, task_metrics, stage)

    except Exception as e:
        import traceback
        err = f"{type(e).__name__}: {e}"
        logger.log(f"执行异常: {err}\n{traceback.format_exc()}")
        metrics = _base_metrics(start_time, stage="exception", error=err)
        try:
            return finish(2, 0, metrics, "exception", err)
        except Exception:
            logger.finish(2, result_detail=0, metrics=metrics)
            raise


def _run_batch_sync(batch_id: int, cancel_event: threading.Event) -> None:
    """同步执行批次（在线程池中运行）。

    - 每次执行追加 attempt，历史结果不删除；
    - 有 manifest 时使用冻结的任务/组变量；
    - 取消后批次状态记为 5（已取消），未执行的下游任务保持未执行。
    """
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

        # 每次整批执行追加一个 attempt，重跑不再覆盖上一轮证据
        attempt = crud.increment_batch_attempt(db, batch_id)
        db.refresh(batch)
        crud.update_batch_status(db, batch_id, 1)  # 执行中

        manifest = manifest_mod.parse_manifest(batch.manifest)
        manifest_ok = (
            bool(manifest)
            and manifest.get("manifest_version") == manifest_mod.MANIFEST_VERSION
            and not manifest.get("error")
        )
        group_vars_override: Optional[dict] = None
        definition_source = "live"
        try:
            if manifest_ok:
                sorted_tasks = _topological_sort(manifest_mod.tasks_from_manifest(manifest))
                group_vars_override = manifest_mod.group_variables_from_manifest(manifest)
                definition_source = "manifest"
            else:
                sorted_tasks = _topological_sort(crud.list_tasks(db, gid=batch.gid))
        except task_graph.DependencyError as exc:
            logger.error("批次 %s 依赖图不合法，未执行: %s", batch_id, exc)
            crud.update_batch_status(db, batch_id, 3)
            return

        # 只清理本轮上下文，上一轮 attempt 目录保留
        runtime.reset_batch(batch.gid, batch.id, attempt)
        runtime.prune_attempts(batch.gid, batch.id, protected={attempt})
        # 保留最近 20 个批次目录；正在整批或单任务执行的批次不能删
        with _running_lock:
            running_ids = set(_running_batches) | {
                bid for bid, _tid in _running_tasks}
        runtime.prune_batches(batch.gid, protected=running_ids)

        total = len(sorted_tasks)
        success_count = 0
        failed_count = 0
        skipped_count = 0

        for task in sorted_tasks:
            if cancel_event.is_set():
                break
            try:
                detail = _execute_single_task(
                    db, batch, subject, group, task, cancel_event,
                    group_vars_override=group_vars_override,
                    definition_source=definition_source,
                )
            except Exception as e:
                import traceback
                err_log = f"执行异常: {e}\n" + traceback.format_exc()
                metrics_text = json.dumps(
                    {"metrics_version": 2, "stage": "exception", "error": str(e)},
                    ensure_ascii=False)
                # 优先复用执行器创建的执行中占位记录，避免留下 result=3 脏数据
                stale = crud.get_batch_task_result(
                    db, batch.id, task.tid, attempt=attempt)
                if stale is not None and stale.result == 3:
                    crud.update_result(
                        db, stale.id, result=2, result_detail=0,
                        metrics=metrics_text, process=err_log)
                else:
                    crud.create_result(
                        db, sid=batch.sid, gid=batch.gid, tid=task.tid,
                        result=2, process=err_log,
                        batch_id=batch.id, result_detail=0,
                        attempt=attempt, metrics=metrics_text,
                    )
                detail = {"success": False, "result_detail": 0, "verdict": "FAIL"}

            if detail.get("verdict") == "SKIP":
                skipped_count += 1
            elif detail.get("result_detail") in (2, 3):
                # UNVALIDATED（detail=3）执行成功但未校验，计为已执行成功
                success_count += 1
            else:
                failed_count += 1
                # 失败任务的下游任务被跳过（因为依赖检查会失败）

        # 更新批次状态；用户取消优先，避免把取消误报为部分成功
        if cancel_event.is_set():
            final_status = 5  # 已取消
        elif failed_count == 0 and success_count == total:
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


def _recompute_batch_status(db: Session, batch_id: int,
                            attempt: Optional[int] = None) -> None:
    """按批次内各任务的最新结果重算批次状态。

    用于单独执行某个任务后同步批次状态（不删除其他任务的结果）。
    指定 attempt 时只统计该轮，避免新旧轮次结果混合。
    """
    batch = crud.get_batch(db, batch_id)
    if batch is None:
        return
    tid_set = {t.tid for t in crud.list_tasks(db, gid=batch.gid)}
    by_tid: dict[int, models.TaskResult] = {}
    results = crud.list_results(
        db, batch_id=batch_id, attempt=attempt if attempt else None)
    for r in results:
        if r.tid in tid_set:
            by_tid.setdefault(r.tid, r)  # list_results 按 id 倒序，首条即最新

    # 调用时机是单任务刚结束（同一批次不允许并发单任务），
    # 因此残留的 result=3 视为未正常结束，不能当作成功。
    if not by_tid:
        status = 0  # 未执行
    elif len(by_tid) >= len(tid_set) and all(r.result == 1 for r in by_tid.values()):
        status = 2  # 全部任务成功
    elif any(r.result == 1 for r in by_tid.values()):
        status = 4  # 部分成功
    else:
        status = 3  # 执行失败
    crud.update_batch_status(db, batch_id, status)


def recompute_batch_status(db: Session, batch_id: int) -> None:
    """对外暴露的批次状态重算（MCP 手工上报结果后调用）。"""
    batch = crud.get_batch(db, batch_id)
    attempt = getattr(batch, "attempt", None) if batch else None
    _recompute_batch_status(db, batch_id, attempt=attempt)


def _run_task_sync(batch_id: int, tid: int, force: bool,
                   cancel_event: threading.Event) -> None:
    """同步执行批次内的单个任务（在线程池中运行）。"""
    db = _get_session_factory()()
    try:
        batch = crud.get_batch(db, batch_id)
        if batch is None:
            return
        subject = crud.get_subject(db, batch.sid)
        group = crud.get_task_group(db, batch.gid)
        task = crud.get_task(db, tid)
        if subject is None or group is None or task is None or task.gid != batch.gid:
            return

        # 单任务调试使用当前定义；与创建批次的 manifest 不一致时显式标记
        manifest = manifest_mod.parse_manifest(batch.manifest)
        frozen_signature = manifest_mod.manifest_task_signature(manifest, tid)
        definition_mismatch = bool(
            frozen_signature and frozen_signature != manifest_mod.task_signature(task))
        if definition_mismatch:
            logger.warning(
                "批次 %s 单任务 %s 使用当前定义执行，与 manifest 冻结定义不一致",
                batch_id, tid)

        try:
            _execute_single_task(
                db, batch, subject, group, task, cancel_event,
                ignore_dependencies=force,
                definition_source="live",
                definition_mismatch=definition_mismatch)
        except Exception as e:
            import traceback
            err_log = f"执行异常: {e}\n" + traceback.format_exc()
            metrics_text = json.dumps(
                {"metrics_version": 2, "stage": "exception", "error": str(e)},
                ensure_ascii=False)
            stale = crud.get_batch_task_result(
                db, batch.id, task.tid, attempt=getattr(batch, "attempt", None))
            if stale is not None and stale.result == 3:
                crud.update_result(
                    db, stale.id, result=2, result_detail=0,
                    metrics=metrics_text, process=err_log)
            else:
                crud.create_result(
                    db, sid=batch.sid, gid=batch.gid, tid=task.tid,
                    result=2, process=err_log,
                    batch_id=batch.id, result_detail=0,
                    attempt=getattr(batch, "attempt", None),
                    metrics=metrics_text,
                )
        finally:
            _recompute_batch_status(db, batch_id, attempt=getattr(batch, "attempt", None))
    finally:
        db.close()
        with _running_lock:
            _running_tasks.pop((batch_id, tid), None)


def has_running_task(batch_id: int, tid: Optional[int] = None) -> bool:
    """批次内是否有任务正在单独执行（指定 tid 时只查该任务）。"""
    with _running_lock:
        if tid is None:
            return any(bid == batch_id for bid, _ in _running_tasks)
        return (batch_id, tid) in _running_tasks


def running_task_tid(batch_id: int) -> Optional[int]:
    """批次内正在单独执行的任务 tid；没有则返回 None。"""
    with _running_lock:
        for bid, tid in _running_tasks:
            if bid == batch_id:
                return tid
    return None


def start_task_async(batch_id: int, tid: int, force: bool = False) -> bool:
    """异步启动批次内的单个任务。返回是否成功启动。

    同一批次同一时刻只允许一个单任务执行，避免共享工作空间冲突。
    """
    with _running_lock:
        if batch_id in _running_batches:
            return False
        if any(bid == batch_id for bid, _ in _running_tasks):
            return False
        cancel_event = threading.Event()
        _running_tasks[(batch_id, tid)] = cancel_event
    _executor.submit(_run_task_sync, batch_id, tid, force, cancel_event)
    return True


def start_batch_async(batch_id: int) -> bool:
    """异步启动批次。返回是否成功启动（已在运行则返回 False）。"""
    with _running_lock:
        if batch_id in _running_batches:
            return False
        if any(bid == batch_id for bid, _ in _running_tasks):
            return False
        cancel_event = threading.Event()
        _running_batches[batch_id] = cancel_event
    _executor.submit(_run_batch_sync, batch_id, cancel_event)
    return True


def cancel_batch(batch_id: int) -> bool:
    """取消正在执行的批次（含该批次正在单独执行的任务）。"""
    with _running_lock:
        events = []
        event = _running_batches.get(batch_id)
        if event is not None:
            events.append(event)
        events.extend(
            ev for (bid, _tid), ev in _running_tasks.items() if bid == batch_id)
    if not events:
        return False
    for ev in events:
        ev.set()
    return True


def reconcile_interrupted(db: Optional[Session] = None) -> dict:
    """启动时对账：把上次进程留下的“执行中”状态标记为中断。

    内存中的运行登记/取消事件在重启后会丢失，若不做对账，批次会长期停在
    “执行中”，也无法按正常流程重跑或取消。这里只处理残留状态，不删除数据。
    """
    own_session = db is None
    session = db or _get_session_factory()()
    try:
        batches = (
            session.query(models.TaskBatch)
            .filter(models.TaskBatch.status == 1)
            .all()
        )
        for batch in batches:
            batch.status = 3  # 执行失败（中断）
        if batches:
            session.commit()

        rows = (
            session.query(models.TaskResult)
            .filter(models.TaskResult.result == 3)
            .all()
        )
        for row in rows:
            try:
                metrics = json.loads(row.metrics) if row.metrics else {}
            except (TypeError, ValueError):
                metrics = {}
            if not isinstance(metrics, dict):
                metrics = {}
            metrics["stage"] = "interrupted"
            metrics["error"] = "服务重启导致执行中断"
            row.result = 2
            row.result_detail = 0
            row.metrics = json.dumps(metrics, ensure_ascii=False)
            row.process = (row.process or "") + "\n=== 服务重启，执行中断，已标记为失败 ==="
        if rows:
            session.commit()
        if batches or rows:
            logger.warning(
                "启动对账：%s 个批次、%s 条执行中结果已标记为中断",
                len(batches), len(rows))
        return {"batches": len(batches), "results": len(rows)}
    finally:
        if own_session:
            session.close()


def is_batch_running(batch_id: int) -> bool:
    with _running_lock:
        return batch_id in _running_batches
