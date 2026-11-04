"""ebaseline 运行上下文 SDK（单文件、仅标准库，兼容 Python 3.9+）。

工作空间内的脚本用它读取当前任务的运行上下文，不必再靠 stdin 或自己查库：

    import ebaseline_sdk as eb

    print(eb.agent_output())          # 当前任务 Agent 的完整回答
    print(eb.stage())                 # pre_script / expect / clean / done
    print(eb.variable("DB2_NODE_SRC.ip"))

    eb.report(ok=True, detail={"db_node": "el7"})   # 输出 JSON 并按 ok 设置退出码

执行器通过环境变量注入上下文位置（脚本无需关心）：

    EBASE_RUN_FILE    当前任务上下文 JSON 的绝对路径
    EBASE_RUN_DIR     当前批次上下文目录
    EBASE_BATCH_ID / EBASE_SID / EBASE_GID / EBASE_TID / EBASE_STAGE
    EBASE_RUNTIME_ROOT   runtime/ 根目录

未注入时（例如在工作空间 Web 终端里手工运行），SDK 会退化为
「该组最近一个任务的上下文」，方便调试。

设计约束：
  - 不 import ebaseline 服务包，也不联网/连库——只读本地 JSON；
  - 因此组独立 venv 不需要安装任何依赖即可使用。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "SDKError",
    "ContextUnavailable",
    "AgentOutputUnavailable",
    "UnknownTask",
    "context",
    "context_path",
    "context_for",
    "agent",
    "agent_output",
    "qa_record",
    "task",
    "metadata",
    "variables",
    "variable",
    "stage",
    "prompt",
    "result",
    "results",
    "available",
    "report",
]

_MISSING = object()


class SDKError(RuntimeError):
    """SDK 使用错误基类。"""


class ContextUnavailable(SDKError):
    """找不到运行上下文（脚本不在执行器里运行，或该组还没有执行记录）。"""


class AgentOutputUnavailable(SDKError):
    """上下文存在，但当前任务还没走到 Agent 阶段。"""


class UnknownTask(SDKError):
    """本批次里没有该任务的上下文文件。"""


_ctx_cache: dict[str, Any] = {}


# ── 路径解析 ───────────────────────────────────────────────────
def _runtime_root() -> Path:
    env = os.environ.get("EBASE_RUNTIME_ROOT")
    if env:
        return Path(env)
    ws = os.environ.get("EBASE_WORKSPACE")
    if ws:
        p = Path(ws).resolve()
        # <root>/workspaces/group_<gid> → <root>/runtime
        if p.parent.name == "workspaces":
            return p.parent.parent / "runtime"
    return Path(__file__).resolve().parent / "runtime"


def _group_id() -> Optional[int]:
    raw = (os.environ.get("EBASE_GID") or "").strip()
    if raw.isdigit():
        return int(raw)
    ws = os.environ.get("EBASE_WORKSPACE")
    if ws:
        name = Path(ws).resolve().name
        if name.startswith("group_") and name[len("group_"):].isdigit():
            return int(name[len("group_"):])
    return None


def _latest_context_file(root: Path, gid: int) -> Optional[Path]:
    group_root = root / "ebase_runs" / f"group_{gid}"
    if not group_root.is_dir():
        return None
    candidates = [p for p in group_root.glob("batch_*/*.json") if not p.name.endswith(".qa.json")]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def context_path() -> Path:
    """解析当前任务上下文 JSON 路径。

    优先级：EBASE_RUN_FILE > EBASE_RUN_DIR + EBASE_TID > 该组最近一次执行。
    """
    direct = os.environ.get("EBASE_RUN_FILE")
    if direct:
        return Path(direct)

    run_dir = os.environ.get("EBASE_RUN_DIR")
    tid = (os.environ.get("EBASE_TID") or "").strip()
    if run_dir and tid:
        return Path(run_dir) / f"{tid}.json"

    gid = _group_id()
    if gid is not None:
        latest = _latest_context_file(_runtime_root(), gid)
        if latest is not None:
            return latest
        raise ContextUnavailable(
            f"任务组 {gid} 还没有执行记录（runtime/ebase_runs/group_{gid}/ 为空）。"
            "请先在本系统里跑一次批次，或显式设置 EBASE_RUN_FILE。"
        )

    raise ContextUnavailable(
        "没有运行上下文：EBASE_RUN_FILE / EBASE_RUN_DIR+EBASE_TID / EBASE_GID 均未设置。"
        "手工调试时可在工作空间终端里设置 EBASE_GID=<gid> 或直接传 tid。"
    )


def _load(path: Path) -> dict:
    try:
        st = path.stat()
    except OSError:
        raise ContextUnavailable(f"上下文文件不存在: {path}") from None
    cache_key = f"{path}:{st.st_mtime_ns}:{st.st_size}"
    cached = _ctx_cache.get("data")
    if cached and cached[0] == cache_key:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SDKError(f"上下文文件无法解析: {path} ({e})") from None
    if not isinstance(data, dict):
        raise SDKError(f"上下文文件格式不正确（顶层应为对象）: {path}")
    _ctx_cache["data"] = (cache_key, data)
    return data


# ── 上下文读取 ─────────────────────────────────────────────────
def context() -> dict:
    """当前任务的完整上下文（dict）。"""
    return _load(context_path())


def context_for(tid: int) -> dict:
    """本批次内指定任务的上下文（用于读取上游任务输出）。"""
    path = Path(context_path()).parent / f"{tid}.json"
    if not path.exists():
        raise UnknownTask(f"本批次没有任务 {tid} 的上下文（{path}）")
    return _load(path)


def available() -> bool:
    """当前是否能解析到上下文。"""
    try:
        context_path()
        return True
    except ContextUnavailable:
        return False


def stage() -> str:
    """当前阶段：pre_script / agent / expect / clean / done，以及提前结束的
    dependency / parse_input。"""
    return str(context().get("stage") or "")


def task() -> dict:
    """当前任务定义（tid/gid/name/depends/pre_script/expect/clean/timeout）。"""
    return context().get("task") or {}


def metadata() -> dict:
    """批次级元信息：batch_id/sid/gid/tid/stage/updated_at。"""
    ctx = context()
    return {
        "batch_id": ctx.get("batch_id"),
        "sid": ctx.get("sid"),
        "gid": ctx.get("gid"),
        "tid": ctx.get("tid"),
        "stage": ctx.get("stage"),
        "updated_at": ctx.get("updated_at"),
    }


def prompt() -> str:
    """本次发送给 Agent 的最终 prompt（已渲染组变量）。"""
    return context().get("prompt") or ""


def variables() -> dict:
    """任务组变量（dict，可能含敏感信息，勿打印到日志）。"""
    return ((context().get("group") or {}).get("variables")) or {}


def variable(path: str, default: Any = _MISSING) -> Any:
    """按路径取组变量：`variable("DB2_NODE_SRC.ip")`。

    组变量没有时回退环境变量（大写、点换下划线），与脚本环境一致。
    """
    cur: Any = variables()
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            cur = _MISSING
            break
    if cur is not _MISSING:
        return cur

    env_key = str(path).upper().replace(".", "_")
    if env_key in os.environ:
        return os.environ[env_key]
    if default is not _MISSING:
        return default
    raise KeyError(f"组变量不存在: {path}")


def agent(tid: Optional[int] = None) -> dict:
    """Agent 结果段（success/output/error/qa_record/tokens/rounds/...）。"""
    ctx = context() if tid is None else context_for(tid)
    return ctx.get("agent") or {}


def agent_output(tid: Optional[int] = None) -> str:
    """当前任务（或指定上游任务）Agent 的完整回答文本。"""
    ctx = context() if tid is None else context_for(tid)
    section = ctx.get("agent")
    if not section:
        label = ctx.get("tid")
        raise AgentOutputUnavailable(f"任务 {label} 尚未执行到 Agent 阶段（stage={ctx.get('stage')}）")
    return section.get("output") or ""


def result(tid: Optional[int] = None) -> dict:
    """任务结果摘要（ok/result_detail/verdict/metrics）。

    任务收尾后返回执行器的最终结论；若当前任务还在跑（例如正在 expect 阶段），
    返回一个临时摘要（`pending=True`），避免脚本拿到空 dict 后误判。
    """
    ctx = context() if tid is None else context_for(tid)
    final = ctx.get("result")
    if final:
        return final

    agent = ctx.get("agent") or {}
    if not agent:
        return {}
    expect = ctx.get("expect") or {}
    stage = ctx.get("stage")
    return {
        "pending": True,
        "ok": None,
        "result": None,
        "result_detail": None,
        "verdict": "RUNNING" if stage in ("agent", "expect") else "PENDING",
        "agent_success": agent.get("success"),
        "expect_success": expect.get("success") if ctx.get("expect") else None,
        "metrics": None,
    }


def results() -> dict:
    """本批次所有任务的上下文摘要，键为 tid（上游未收尾时给临时状态）。"""
    cur_path = Path(context_path())
    out: dict = {}
    for p in sorted(cur_path.parent.glob("*.json")):
        if p.name.endswith(".qa.json"):
            continue
        try:
            data = _load(p)
        except SDKError:
            continue
        final = data.get("result") or {}
        if final:
            ok, verdict = final.get("ok"), final.get("verdict")
        elif data.get("agent"):
            ok = None
            verdict = "RUNNING" if data.get("stage") in ("agent", "expect") else "PENDING"
        else:
            ok, verdict = None, "PENDING"
        out[int(data.get("tid", p.stem))] = {
            "stage": data.get("stage"),
            "ok": ok,
            "verdict": verdict,
        }
    return out


def qa_record(tid: Optional[int] = None) -> Optional[dict]:
    """QA 闭环记录（探测到的字段、喂回的字段、追问轮数）。

    依次尝试：上下文里的 agent.qa_record → EBASE_QA_FILE → I2STREAM_AGENT_QA_FILE。
    都不存在时返回 None。
    """
    try:
        record = agent(tid).get("qa_record")
        if isinstance(record, dict) and record:
            return record
    except SDKError:
        pass

    for env_key in ("EBASE_QA_FILE", "I2STREAM_AGENT_QA_FILE"):
        raw = os.environ.get(env_key)
        if not raw:
            continue
        path = Path(raw)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


# ── 结果上报 ───────────────────────────────────────────────────
def report(ok: bool, detail: Optional[dict] = None, *, exit: bool = True) -> bool:
    """打印统一格式的校验结论，默认按 ok 设置进程退出码。

    expect 脚本用它即可：eb.report(ok=status == "NORMAL", detail={...})
    """
    payload: dict = {"check_result": "PASS" if ok else "FAIL"}
    if detail:
        payload.update(detail)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if exit:
        raise SystemExit(0 if ok else 1)
    return ok


# ── 命令行调试入口 ─────────────────────────────────────────────
def _main(argv: list[str]) -> int:
    dump = "--dump" in argv
    tids = [a for a in argv if a.isdigit()]
    try:
        tid = int(tids[0]) if tids else None
        ctx = context_for(tid) if tid is not None else context()
    except SDKError as e:
        print(f"[ebaseline-sdk] {e}", file=sys.stderr)
        return 2

    if dump:
        print(json.dumps(ctx, ensure_ascii=False, indent=2))
        return 0

    agent_info = ctx.get("agent") or {}
    print(f"context : {context_path()}")
    print(f"batch   : {ctx.get('batch_id')}  sid={ctx.get('sid')}  gid={ctx.get('gid')}  tid={ctx.get('tid')}")
    print(f"task    : {(ctx.get('task') or {}).get('name')}")
    print(f"stage   : {ctx.get('stage')}")
    print(f"agent   : success={agent_info.get('success')} rounds={agent_info.get('rounds')} "
          f"tools={agent_info.get('tool_calls')}")
    output = agent_info.get("output") or ""
    print(f"output  : {len(output)} 字符")
    if output:
        preview = output[:800]
        print("--- preview ---")
        print(preview + ("..." if len(output) > len(preview) else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
