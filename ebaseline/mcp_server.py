"""MCP 服务：为智能体暴露测试任务操作工具集。

使用官方 mcp SDK 的 FastMCP，支持 streamable HTTP。
智能体可通过 MCP 客户端连接本服务，执行：
  - 浏览/管理任务组、任务、待测主体
  - 创建/启动/取消批次，单独执行批次内任务
  - 查询批次状态/详情/结果、导出 CSV
  - 上报任务结果
  - 管理任务组工作空间文件

说明：认证/用户管理（MCP 连接没有请求级用户上下文）和 WebSocket 终端
不暴露为 MCP 工具，其余 REST 能力均有对应工具。
"""
from __future__ import annotations

import csv
import io
import json
import shutil
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException
from mcp.server import FastMCP
from sqlalchemy.orm import Session

from . import (
    crud,
    executor,
    manifest as manifest_mod,
    mcp_auth,
    models,
    runtime,
    schemas,
    task_graph,
)
from .config import get_config
from .database import _get_session_factory
from .executor import render_variables
from .metrics import (
    compute_batch_metrics,
    latest_results_by_tid,
    pending_summary,
    result_summary,
)
from .routers.workspace import _safe_resolve, _workspace_dir, delete_workspace
from .security import mask_variables

mcp = FastMCP("ebaseline", stateless_http=True)

_STATUS_TEXT = {
    0: "未执行", 1: "执行中", 2: "已完成", 3: "执行失败", 4: "部分成功",
    5: "已取消",
}


def _get_db() -> Session:
    """获取数据库会话（每次调用新建，避免线程安全问题）。"""
    return _get_session_factory()()


def _dump(data: Any) -> str:
    """统一 MCP 工具的 JSON 输出格式。"""
    return json.dumps(data, ensure_ascii=False, indent=2)


def _parse_depends(depends_str: Optional[str]) -> Optional[list[int]]:
    """把 task_list.depends（逗号分隔）解析为 tid 列表。"""
    if not depends_str:
        return None
    try:
        return task_graph.parse_depends(depends_str) or None
    except task_graph.DependencyError:
        return None


def _task_to_dict(task: models.TaskList) -> dict:
    depends = _parse_depends(task.depends)
    input_data = None
    if task.input:
        try:
            input_data = json.loads(task.input)
        except json.JSONDecodeError:
            input_data = {"prompt": task.input}
    return {
        "tid": task.tid,
        "gid": task.gid,
        "name": task.name,
        "description": task.description,
        "depends": depends,
        "pre_script": task.pre_script,
        "input": input_data,
        "timeout": task.timeout,
        "expect": task.expect,
        "clean": task.clean,
    }


def _group_to_dict(group: models.TaskGroup) -> dict:
    variables = None
    if group.variables:
        try:
            variables = json.loads(group.variables)
        except json.JSONDecodeError:
            variables = group.variables
    cfg = get_config()
    if variables is not None and cfg.security_mask_group_secrets:
        variables = mask_variables(variables, cfg.security_mask_placeholder)
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "variables": variables,
        "python_version": group.python_version,
    }


def _subject_to_dict(subject: models.Subject) -> dict:
    return {
        "id": subject.id,
        "name": subject.name,
        "ipaddr": subject.ipaddr,
        "port": subject.port,
        "api_mode": subject.api_mode,
    }


def _result_to_dict(result: models.TaskResult, max_process: Optional[int] = 2000) -> dict:
    """单条结果转 dict；max_process=None 时返回完整日志。"""
    summary = result_summary(result)
    process = result.process or ""
    truncated = max_process is not None and len(process) > max_process
    if truncated:
        process = process[:max_process]
    return {
        "id": result.id,
        "batch_id": result.batch_id,
        "sid": result.sid,
        "gid": result.gid,
        "tid": result.tid,
        "result": result.result,
        "result_detail": result.result_detail,
        "attempt": result.attempt,
        "verdict": summary["verdict"],
        "verdict_text": summary["verdict_text"],
        "metrics": summary,
        "process": process,
        "process_truncated": truncated,
        "create_time": result.create_time.isoformat() if result.create_time else None,
    }


def _batch_to_dict(batch: models.TaskBatch, metrics: Optional[dict] = None) -> dict:
    return {
        "batch_id": batch.id,
        "sid": batch.sid,
        "gid": batch.gid,
        "status": batch.status,
        "status_text": _STATUS_TEXT.get(batch.status, "未知"),
        "attempt": getattr(batch, "attempt", 0) or 0,
        "create_time": batch.create_time.isoformat() if batch.create_time else None,
        "metrics": metrics or {},
    }


def _batch_metrics_map(db: Session, batches: list[models.TaskBatch]) -> dict[int, dict]:
    """一次查询多个批次的指标，避免逐批次查询。"""
    batch_ids = [b.id for b in batches]
    results = crud.list_results_for_batches(db, batch_ids)
    by_batch: dict[int, list[models.TaskResult]] = {}
    for r in results:
        if r.batch_id is not None:
            by_batch.setdefault(r.batch_id, []).append(r)
    task_counts = crud.count_tasks_by_gid(db, {b.gid for b in batches})
    out: dict[int, dict] = {}
    for b in batches:
        rows = by_batch.get(b.id, [])
        if getattr(b, "attempt", 0) and b.attempt > 0:
            rows = [r for r in rows if r.attempt == b.attempt]
        out[b.id] = compute_batch_metrics(
            rows, task_count=task_counts.get(b.gid, 0))
    return out


def _build_task_input(
    input_data: Optional[dict],
    prompt: Optional[str],
    conversation: Optional[list],
) -> Optional[dict]:
    """优先使用原始 input；否则用 prompt/conversation 组装。"""
    if input_data is not None:
        return input_data
    if prompt is None and conversation is None:
        return None
    data: dict[str, Any] = {}
    if prompt is not None:
        data["prompt"] = prompt
    if conversation is not None:
        data["conversation"] = conversation
    return data


def _results_csv_text(results: list[models.TaskResult]) -> str:
    """结果列表导出为 CSV 文本（与 REST /results/export 列一致）。"""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "batch_id", "attempt", "sid", "gid", "tid", "result", "result_detail",
        "verdict", "duration_sec", "agent_duration_sec", "rounds", "tool_calls",
        "tokens_input", "tokens_output", "tokens_total", "first_token_ms",
        "process", "create_time",
    ])
    for r in results:
        m = result_summary(r)
        tokens = m.get("tokens") or {}
        writer.writerow([
            r.id, r.batch_id, r.attempt, r.sid, r.gid, r.tid, r.result, r.result_detail,
            m.get("verdict"), m.get("duration_sec"), m.get("agent_duration_sec"),
            m.get("rounds"), m.get("tool_calls"),
            tokens.get("input"), tokens.get("output"), tokens.get("total"),
            m.get("first_token_ms"),
            (r.process or "").replace("\n", "\\n"),
            r.create_time.isoformat() if r.create_time else "",
        ])
    return output.getvalue()


def _workspace_group_error(db: Session, gid: int) -> Optional[str]:
    if crud.get_task_group(db, gid) is None:
        return f"任务组 {gid} 不存在"
    return None


def _resolve_workspace_path(gid: int, path: str) -> tuple[Optional[Path], Optional[str]]:
    """解析工作空间路径，把越界异常转成错误文本。"""
    try:
        return _safe_resolve(gid, path), None
    except HTTPException as exc:
        return None, str(exc.detail)


# ── 任务组 ─────────────────────────────────────────────────────

@mcp.tool()
def list_task_groups() -> str:
    """列出所有任务组。返回 JSON 数组，每个元素含 id/name/description/variables/python_version。"""
    db = _get_db()
    try:
        groups = crud.list_task_groups(db)
        return _dump([_group_to_dict(g) for g in groups])
    finally:
        db.close()


@mcp.tool()
def get_task_group(gid: int) -> str:
    """获取任务组详情（含 variables 变量定义和 python_version）。"""
    db = _get_db()
    try:
        group = crud.get_task_group(db, gid)
        if group is None:
            return _dump({"error": f"任务组 {gid} 不存在"})
        return _dump(_group_to_dict(group))
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def create_task_group(
    name: str,
    description: Optional[str] = None,
    variables: Optional[dict[str, Any]] = None,
    python_version: str = "3.11",
) -> str:
    """创建任务组。variables 为组变量 JSON 对象，python_version 用于组工作空间 venv。"""
    db = _get_db()
    try:
        data = schemas.TaskGroupCreate(
            name=name,
            description=description,
            variables=variables,
            python_version=python_version,
        )
        group = crud.create_task_group(db, data)
        return _dump(_group_to_dict(group))
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def update_task_group(
    gid: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    variables: Optional[dict[str, Any]] = None,
    python_version: Optional[str] = None,
    clear_fields: Optional[list[str]] = None,
) -> str:
    """更新任务组。参数为 None 表示不修改；variables 传 {} 可清空变量。

    clear_fields 可显式清空 description/variables（工具参数无法区分
    “未提供”和 null，因此需要单独的清除列表）。
    """
    db = _get_db()
    try:
        fields: dict[str, Any] = {}
        clear = set(clear_fields or [])
        if name is not None:
            fields["name"] = name
        if description is not None:
            fields["description"] = description
        elif "description" in clear:
            fields["description"] = None
        if variables is not None:
            fields["variables"] = variables
        elif "variables" in clear:
            fields["variables"] = None
        if python_version is not None:
            fields["python_version"] = python_version
        group = crud.update_task_group(db, gid, schemas.TaskGroupUpdate(**fields))
        if group is None:
            return _dump({"error": f"任务组 {gid} 不存在"})
        return _dump(_group_to_dict(group))
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def delete_task_group(gid: int) -> str:
    """删除任务组（级联删除组内任务、批次与结果，并清理工作空间和运行上下文）。"""
    db = _get_db()
    try:
        if not crud.delete_task_group(db, gid):
            return _dump({"error": f"任务组 {gid} 不存在"})
    finally:
        db.close()

    warnings: list[str] = []
    try:
        delete_workspace(gid)
    except OSError as exc:
        warnings.append(f"清理工作空间失败: {exc}")
    try:
        runtime.purge_group(gid)
    except OSError as exc:
        warnings.append(f"清理运行上下文失败: {exc}")
    return _dump({"gid": gid, "deleted": True, "warnings": warnings})


# ── 任务 ───────────────────────────────────────────────────────

@mcp.tool()
def list_tasks_in_group(gid: int) -> str:
    """列出指定任务组内的所有任务。"""
    db = _get_db()
    try:
        if crud.get_task_group(db, gid) is None:
            return _dump({"error": f"任务组 {gid} 不存在"})
        tasks = crud.list_tasks(db, gid=gid)
        return _dump([
            {"tid": t.tid, "gid": t.gid, "name": t.name, "description": t.description,
             "depends": _parse_depends(t.depends), "timeout": t.timeout}
            for t in tasks
        ])
    finally:
        db.close()


@mcp.tool()
def get_task_detail(tid: int) -> str:
    """获取任务完整定义（含 prompt 模板、依赖、脚本配置等）。"""
    db = _get_db()
    try:
        task = crud.get_task(db, tid)
        if task is None:
            return _dump({"error": f"任务 {tid} 不存在"})
        return _dump(_task_to_dict(task))
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def render_task_prompt(tid: int) -> str:
    """用任务组变量渲染任务 prompt，返回可直接发给 Agent 的最终文本。"""
    db = _get_db()
    try:
        task = crud.get_task(db, tid)
        if task is None:
            return _dump({"error": f"任务 {tid} 不存在"})
        group = crud.get_task_group(db, task.gid)
        if group is None:
            return _dump({"error": f"任务组 {task.gid} 不存在"})

        input_data = {}
        if task.input:
            try:
                input_data = json.loads(task.input)
            except json.JSONDecodeError:
                pass

        prompt_template = input_data.get("prompt", "")
        group_vars = {}
        if group.variables:
            try:
                group_vars = json.loads(group.variables)
            except json.JSONDecodeError:
                pass

        prompt = render_variables(prompt_template, group_vars)
        return _dump({
            "tid": tid,
            "prompt": prompt,
            "conversation": input_data.get("conversation", []),
        })
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def create_task(
    gid: int,
    name: str,
    description: Optional[str] = None,
    depends: Optional[list[int]] = None,
    pre_script: Optional[str] = None,
    prompt: Optional[str] = None,
    conversation: Optional[list[dict[str, Any]]] = None,
    input: Optional[dict[str, Any]] = None,
    timeout: Optional[int] = None,
    expect: Optional[str] = None,
    clean: Optional[str] = None,
    tid: Optional[int] = None,
) -> str:
    """在组内创建任务。

    prompt/conversation 是 input 字段的便捷写法；也可以直接传 input 对象
    （{"prompt": "...", "conversation": [...]}），两者都传时以 input 为准。
    tid 不填时自动分配（gid * 10000 + 组内序号）。
    """
    db = _get_db()
    try:
        if crud.get_task_group(db, gid) is None:
            return _dump({"error": f"任务组 {gid} 不存在"})
        data = schemas.TaskCreate(
            tid=tid,
            name=name,
            description=description,
            depends=depends,
            pre_script=pre_script,
            input=_build_task_input(input, prompt, conversation),
            timeout=timeout,
            expect=expect,
            clean=clean,
        )
        try:
            task = crud.create_task(db, gid, data)
        except task_graph.DependencyError as exc:
            return _dump({"error": "依赖不合法", "issues": exc.issues})
        return _dump(_task_to_dict(task))
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def update_task(
    tid: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    depends: Optional[list[int]] = None,
    pre_script: Optional[str] = None,
    prompt: Optional[str] = None,
    conversation: Optional[list[dict[str, Any]]] = None,
    input: Optional[dict[str, Any]] = None,
    timeout: Optional[int] = None,
    expect: Optional[str] = None,
    clean: Optional[str] = None,
    clear_fields: Optional[list[str]] = None,
) -> str:
    """更新任务。参数为 None 表示不修改；depends 传 [] 可清空依赖。

    prompt/conversation 是 input 字段的便捷写法，两者都传时以 input 为准。
    clear_fields 可显式清空 description/depends/pre_script/input/timeout/
    expect/clean（工具参数无法区分“未提供”和 null）。
    """
    db = _get_db()
    try:
        if crud.get_task(db, tid) is None:
            return _dump({"error": f"任务 {tid} 不存在"})
        fields: dict[str, Any] = {}
        clear = set(clear_fields or [])
        if name is not None:
            fields["name"] = name
        if description is not None:
            fields["description"] = description
        elif "description" in clear:
            fields["description"] = None
        if depends is not None:
            fields["depends"] = depends
        elif "depends" in clear:
            fields["depends"] = None
        if pre_script is not None:
            fields["pre_script"] = pre_script
        elif "pre_script" in clear:
            fields["pre_script"] = None
        new_input = _build_task_input(input, prompt, conversation)
        if new_input is not None:
            fields["input"] = new_input
        elif "input" in clear:
            fields["input"] = None
        if timeout is not None:
            fields["timeout"] = timeout
        elif "timeout" in clear:
            fields["timeout"] = None
        if expect is not None:
            fields["expect"] = expect
        elif "expect" in clear:
            fields["expect"] = None
        if clean is not None:
            fields["clean"] = clean
        elif "clean" in clear:
            fields["clean"] = None
        try:
            task = crud.update_task(db, tid, schemas.TaskUpdate(**fields))
        except task_graph.DependencyError as exc:
            return _dump({"error": "依赖不合法", "issues": exc.issues})
        if task is None:
            return _dump({"error": f"任务 {tid} 不存在"})
        return _dump(_task_to_dict(task))
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def delete_task(tid: int) -> str:
    """删除任务。"""
    db = _get_db()
    try:
        if not crud.delete_task(db, tid):
            return _dump({"error": f"任务 {tid} 不存在"})
        return _dump({"tid": tid, "deleted": True})
    finally:
        db.close()


# ── 待测主体 ───────────────────────────────────────────────────

@mcp.tool()
def list_subjects() -> str:
    """列出所有待测 agent。"""
    db = _get_db()
    try:
        subjects = crud.list_subjects(db)
        return _dump([_subject_to_dict(s) for s in subjects])
    finally:
        db.close()


@mcp.tool()
def get_subject(sid: int) -> str:
    """获取待测 agent 详情（不含 secret）。"""
    db = _get_db()
    try:
        subject = crud.get_subject(db, sid)
        if subject is None:
            return _dump({"error": f"待测主体 {sid} 不存在"})
        return _dump(_subject_to_dict(subject))
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def create_subject(
    name: str,
    ipaddr: str,
    port: int,
    secret: str = "",
    api_mode: int = 0,
) -> str:
    """注册待测 agent。api_mode: 0=OpenAI Responses API。"""
    db = _get_db()
    try:
        data = schemas.SubjectCreate(
            name=name, ipaddr=ipaddr, port=port, secret=secret, api_mode=api_mode,
        )
        subject = crud.create_subject(db, data)
        return _dump(_subject_to_dict(subject))
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def update_subject(
    sid: int,
    name: Optional[str] = None,
    ipaddr: Optional[str] = None,
    port: Optional[int] = None,
    secret: Optional[str] = None,
    api_mode: Optional[int] = None,
) -> str:
    """更新待测 agent。参数为 None 表示不修改。"""
    db = _get_db()
    try:
        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = name
        if ipaddr is not None:
            fields["ipaddr"] = ipaddr
        if port is not None:
            fields["port"] = port
        if secret is not None:
            fields["secret"] = secret
        if api_mode is not None:
            fields["api_mode"] = api_mode
        subject = crud.update_subject(db, sid, schemas.SubjectUpdate(**fields))
        if subject is None:
            return _dump({"error": f"待测主体 {sid} 不存在"})
        return _dump(_subject_to_dict(subject))
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def delete_subject(sid: int) -> str:
    """删除待测 agent。"""
    db = _get_db()
    try:
        if not crud.delete_subject(db, sid):
            return _dump({"error": f"待测主体 {sid} 不存在"})
        return _dump({"sid": sid, "deleted": True})
    finally:
        db.close()


# ── 批次 ───────────────────────────────────────────────────────

@mcp.tool()
@mcp_auth.require_admin
def create_batch(sid: int, gid: int) -> str:
    """创建测试批次。返回批次 id，可用于后续启动和查询。"""
    db = _get_db()
    try:
        if crud.get_subject(db, sid) is None:
            return _dump({"error": f"待测主体 {sid} 不存在"})
        if crud.get_task_group(db, gid) is None:
            return _dump({"error": f"任务组 {gid} 不存在"})
        issues = task_graph.validate_tasks(crud.list_tasks(db, gid=gid))
        if issues:
            return _dump({"error": "依赖图不合法", "issues": issues})
        batch = crud.create_batch(db, sid, gid)
        return _dump(_batch_to_dict(batch))
    finally:
        db.close()


@mcp.tool()
def list_batches(skip: int = 0, limit: int = 100) -> str:
    """列出批次（按 id 倒序，含聚合指标）。"""
    db = _get_db()
    try:
        batches = crud.list_batches(db, skip, limit)
        metrics_map = _batch_metrics_map(db, batches)
        return _dump([_batch_to_dict(b, metrics_map[b.id]) for b in batches])
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def start_batch(batch_id: int) -> str:
    """启动批次执行（异步，立即返回）。批次将在后台线程中执行所有任务。"""
    db = _get_db()
    try:
        batch = crud.get_batch(db, batch_id)
        if batch is None:
            return _dump({"error": f"批次 {batch_id} 不存在"})
        if batch.status == 1:
            return _dump({"error": "批次已在执行中"})
        if executor.has_running_task(batch_id):
            return _dump({"error": "该批次有任务正在单独执行，请等待完成"})
        manifest = manifest_mod.parse_manifest(batch.manifest)
        manifest_ok = bool(
            manifest
            and manifest.get("manifest_version") == manifest_mod.MANIFEST_VERSION
            and not manifest.get("error")
        )
        tasks = (manifest_mod.tasks_from_manifest(manifest) if manifest_ok
                 else crud.list_tasks(db, gid=batch.gid))
        issues = task_graph.validate_tasks(tasks)
        if issues:
            return _dump({"error": "依赖图不合法，无法执行", "issues": issues})
        if not tasks:
            return _dump({"error": "批次没有可执行的任务"})
        if not executor.start_batch_async(batch_id):
            return _dump({"error": "批次已在执行队列中"})
        return _dump({
            "batch_id": batch_id,
            "status": 1,
            "message": "批次已启动",
        })
    finally:
        db.close()


@mcp.tool()
def get_batch_status(batch_id: int) -> str:
    """查询批次状态。status: 0=未执行, 1=执行中, 2=已完成, 3=执行失败, 4=部分成功。"""
    db = _get_db()
    try:
        batch = crud.get_batch(db, batch_id)
        if batch is None:
            return _dump({"error": f"批次 {batch_id} 不存在"})
        data = _batch_to_dict(batch)
        data["running_task_tid"] = executor.running_task_tid(batch_id)
        return _dump(data)
    finally:
        db.close()


@mcp.tool()
def get_batch_detail(batch_id: int, attempt: Optional[int] = None) -> str:
    """获取批次详情：主体/任务组名称、聚合指标、每任务结果摘要、正在单独执行的任务。

    attempt 为空查看最新一轮；指定 attempt 查看该轮历史结果。
    """
    db = _get_db()
    try:
        batch = crud.get_batch(db, batch_id)
        if batch is None:
            return _dump({"error": f"批次 {batch_id} 不存在"})

        subject = crud.get_subject(db, batch.sid)
        group = crud.get_task_group(db, batch.gid)
        manifest = manifest_mod.parse_manifest(batch.manifest)
        manifest_ok = bool(
            manifest
            and manifest.get("manifest_version") == manifest_mod.MANIFEST_VERSION
            and not manifest.get("error")
        )
        tasks = (manifest_mod.tasks_from_manifest(manifest) if manifest_ok
                 else crud.list_tasks(db, gid=batch.gid))
        effective_attempt = batch.attempt if attempt is None else attempt
        # 显式 attempt=0 要过滤到第 0 轮；默认且批次未跑过时才取全部
        attempt_filter = (
            effective_attempt
            if attempt is not None or (batch.attempt or 0) > 0
            else None)
        results = crud.list_results(
            db, batch_id=batch.id, attempt=attempt_filter)
        # list_results 按 id 倒序，每 tid 取最新一条
        by_tid: dict[int, models.TaskResult] = {}
        for r in results:
            by_tid.setdefault(r.tid, r)

        task_details = []
        for task in tasks:
            r = by_tid.get(task.tid)
            summary = result_summary(r) if r else pending_summary()
            process = (r.process or "")[:2000] if r else None
            task_details.append({
                "tid": task.tid,
                "name": task.name,
                "depends": _parse_depends(task.depends),
                "pre_script": task.pre_script,
                "expect": task.expect,
                "clean": task.clean,
                "timeout": task.timeout,
                "result": r.result if r else 0,
                "result_detail": r.result_detail if r else None,
                "attempt": r.attempt if r else (batch.attempt or 0),
                "verdict": summary["verdict"],
                "verdict_text": summary["verdict_text"],
                "result_text": summary["verdict_text"],
                "process": process,
                "process_truncated": bool(r and r.process and len(r.process) > 2000),
                "create_time": r.create_time.isoformat() if r else None,
                "result_id": r.id if r else None,
                "metrics": summary,
                "definition_source": "manifest" if manifest_ok else "live",
            })

        data = _batch_to_dict(
            batch,
            compute_batch_metrics(results, task_count=len(tasks)),
        )
        available = sorted(set(crud.list_batch_attempts(db, batch.id)) | {batch.attempt or 0})
        cfg = get_config()
        data.update({
            "subject_name": subject.name if subject else None,
            "group_name": group.name if group else None,
            "tasks": task_details,
            "running_task_tid": executor.running_task_tid(batch.id),
            "viewing_attempt": effective_attempt,
            "available_attempts": available,
            "manifest_summary": (
                manifest_mod.manifest_summary(manifest, cfg.security_mask_placeholder)
                if manifest_ok else None),
        })
        return _dump(data)
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def run_batch_task(batch_id: int, tid: int, force: bool = False) -> str:
    """异步执行批次内的单个任务。force=true 时忽略依赖检查（用于单独调试）。"""
    db = _get_db()
    try:
        batch = crud.get_batch(db, batch_id)
        if batch is None:
            return _dump({"error": f"批次 {batch_id} 不存在"})
        if batch.status == 1 or executor.is_batch_running(batch_id):
            return _dump({"error": "批次正在执行中"})
        if crud.get_subject(db, batch.sid) is None:
            return _dump({"error": "批次关联的待测主体不存在"})
        task = crud.get_task(db, tid)
        if task is None or task.gid != batch.gid:
            return _dump({"error": f"任务 {tid} 不存在或不属于该批次任务组"})
        if executor.has_running_task(batch_id):
            return _dump({"error": "该批次有其他任务正在执行，请等待完成"})
        if not executor.start_task_async(batch_id, tid, force):
            return _dump({"error": "任务已在执行队列中"})
        return _dump({
            "batch_id": batch_id,
            "tid": tid,
            "force": force,
            "message": "任务已启动",
        })
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def cancel_batch(batch_id: int) -> str:
    """取消正在执行的批次。"""
    db = _get_db()
    try:
        if crud.get_batch(db, batch_id) is None:
            return _dump({"error": f"批次 {batch_id} 不存在"})
        if not executor.cancel_batch(batch_id):
            return _dump({"error": "批次未在执行中"})
        return _dump({"batch_id": batch_id, "message": "已发送取消信号"})
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def delete_batch(batch_id: int) -> str:
    """删除批次（只删除该批次自己的结果，不影响同主体/同组的其他批次）。"""
    db = _get_db()
    try:
        if not crud.delete_batch(db, batch_id):
            return _dump({"error": f"批次 {batch_id} 不存在"})
        return _dump({"batch_id": batch_id, "deleted": True})
    finally:
        db.close()


@mcp.tool()
def get_task_results(batch_id: int, attempt: Optional[int] = None) -> str:
    """查询批次中所有任务的执行结果；attempt 为空查看最新一轮。"""
    db = _get_db()
    try:
        batch = crud.get_batch(db, batch_id)
        if batch is None:
            return _dump({"error": f"批次 {batch_id} 不存在"})
        manifest = manifest_mod.parse_manifest(batch.manifest)
        manifest_ok = bool(
            manifest
            and manifest.get("manifest_version") == manifest_mod.MANIFEST_VERSION
            and not manifest.get("error")
        )
        tasks = (manifest_mod.tasks_from_manifest(manifest) if manifest_ok
                 else crud.list_tasks(db, gid=batch.gid))
        effective_attempt = batch.attempt if attempt is None else attempt
        attempt_filter = (
            effective_attempt
            if attempt is not None or (batch.attempt or 0) > 0
            else None)
        results = []
        for task in tasks:
            r = crud.get_batch_task_result(
                db, batch_id, task.tid, attempt=attempt_filter)
            summary = result_summary(r) if r else None
            results.append({
                "tid": task.tid,
                "name": task.name,
                "result": r.result if r else 0,
                "result_detail": r.result_detail if r else None,
                "attempt": r.attempt if r else (batch.attempt or 0),
                "verdict": summary["verdict"] if summary else "PENDING",
                "verdict_text": summary["verdict_text"] if summary else "未执行",
                "metrics": summary,
                "result_id": r.id if r else None,
                "process": (r.process or "")[:2000] if r else None,
                "create_time": r.create_time.isoformat() if r else None,
            })
        return _dump(results)
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def submit_task_result(
    batch_id: int,
    tid: int,
    result: int,
    process: Optional[str] = None,
    validated: bool = False,
) -> str:
    """智能体主动上报任务结果。result: 0=未执行, 1=成功, 2=失败。

    validated=False（默认）时 result=1 记为“未校验”，不能算质量通过；
    只有明确声明已执行校验时才记 PASS。
    """
    db = _get_db()
    try:
        if result not in (0, 1, 2):
            return _dump({"error": "result 只能为 0/1/2"})
        batch = crud.get_batch(db, batch_id)
        if batch is None:
            return _dump({"error": f"批次 {batch_id} 不存在"})
        task = crud.get_task(db, tid)
        if task is None:
            return _dump({"error": f"任务 {tid} 不存在"})
        if task.gid != batch.gid:
            return _dump({"error": f"任务 {tid} 不属于批次 {batch_id} 的任务组"})

        if result == 1:
            detail = 2 if validated else 3
            expect_success = True if validated else None
        elif result == 2:
            detail = 0
            expect_success = False if validated else None
        else:
            detail = None
            expect_success = None

        r = crud.create_result(
            db,
            sid=batch.sid,
            gid=batch.gid,
            tid=tid,
            result=result,
            process=process,
            batch_id=batch_id,
            result_detail=detail,
            attempt=getattr(batch, "attempt", None),
            metrics=json.dumps({
                "metrics_version": 2,
                "stage": "manual",
                "agent_success": result == 1,
                "expect_configured": bool(validated),
                "expect_success": expect_success,
                "validated": bool(validated and result == 1),
            }, ensure_ascii=False),
        )
        executor.recompute_batch_status(db, batch_id)
        return _dump({
            "result_id": r.id,
            "batch_id": batch_id,
            "tid": tid,
            "result": result,
            "validated": bool(validated),
            "attempt": getattr(batch, "attempt", None),
            "message": "结果已记录",
        })
    finally:
        db.close()


@mcp.tool()
def export_batch_csv(batch_id: int, attempt: Optional[int] = None) -> str:
    """导出批次结果为 CSV 格式文本（同一任务重跑时取最新结果）。

    attempt 为空时导出最新结果；指定 attempt 时只导出该轮。
    """
    db = _get_db()
    try:
        if crud.get_batch(db, batch_id) is None:
            return _dump({"error": f"批次 {batch_id} 不存在"})
        results = latest_results_by_tid(
            crud.list_results(db, batch_id=batch_id, attempt=attempt))
        if not results:
            return _dump({"error": "批次无结果"})
        return _results_csv_text(results)
    finally:
        db.close()


# ── 结果 ───────────────────────────────────────────────────────

@mcp.tool()
def list_results(
    sid: Optional[int] = None,
    gid: Optional[int] = None,
    tid: Optional[int] = None,
    result: Optional[int] = None,
    batch_id: Optional[int] = None,
    attempt: Optional[int] = None,
    skip: int = 0,
    limit: int = 500,
) -> str:
    """查询执行结果，支持按 sid/gid/tid/result/batch_id/attempt 过滤，按 id 倒序分页。

    返回 {"total": 匹配总数, "returned": 本次返回条数, "results": [...]}；
    process 默认截断到 2000 字符，完整日志用 get_result。
    """
    db = _get_db()
    try:
        results = crud.list_results(
            db, sid=sid, gid=gid, tid=tid, result=result, batch_id=batch_id,
            attempt=attempt,
        )
        total = len(results)
        skip = max(0, skip)
        page = results[skip: skip + limit] if limit > 0 else results[skip:]
        return _dump({
            "total": total,
            "returned": len(page),
            "results": [_result_to_dict(r) for r in page],
        })
    finally:
        db.close()


@mcp.tool()
def get_result(result_id: int) -> str:
    """获取单条结果详情（含完整 process 日志和指标）。"""
    db = _get_db()
    try:
        r = crud.get_result(db, result_id)
        if r is None:
            return _dump({"error": f"结果 {result_id} 不存在"})
        return _dump(_result_to_dict(r, max_process=None))
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def delete_results(result_ids: list[int]) -> str:
    """批量删除结果。result_ids 为结果 id 列表。"""
    db = _get_db()
    try:
        ids = [int(x) for x in result_ids]
        if not ids:
            return _dump({"error": "result_ids 不能为空"})
        deleted = (
            db.query(models.TaskResult)
            .filter(models.TaskResult.id.in_(ids))
            .delete(synchronize_session=False)
        )
        db.commit()
        return _dump({"deleted": deleted, "requested": len(ids)})
    finally:
        db.close()


@mcp.tool()
def export_results_csv(
    sid: Optional[int] = None,
    gid: Optional[int] = None,
    batch_id: Optional[int] = None,
    attempt: Optional[int] = None,
) -> str:
    """按条件导出结果为 CSV 文本（指定 batch_id 时同一任务取最新结果）。"""
    db = _get_db()
    try:
        results = crud.list_results(
            db, sid=sid, gid=gid, batch_id=batch_id, attempt=attempt)
        if batch_id is not None:
            results = latest_results_by_tid(results)
        if not results:
            return _dump({"error": "没有可导出的结果"})
        return _results_csv_text(results)
    finally:
        db.close()


# ── 工作空间文件 ───────────────────────────────────────────────

@mcp.tool()
def list_workspace_files(gid: int, path: str = "") -> str:
    """列出任务组工作空间目录内容。path 为相对工作空间根目录的路径，空表示根目录。"""
    db = _get_db()
    try:
        error = _workspace_group_error(db, gid)
        if error:
            return _dump({"error": error})
        target, error = _resolve_workspace_path(gid, path or ".")
        if error:
            return _dump({"error": error})
        if not target.exists():
            return _dump({"error": f"路径不存在: {path}"})
        if not target.is_dir():
            return _dump({"error": f"不是目录: {path}"})

        base = _workspace_dir(gid)
        entries = []
        for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
            st = item.stat()
            entries.append({
                "name": item.name,
                "path": str(item.relative_to(base)),
                "is_dir": item.is_dir(),
                "size": st.st_size if item.is_file() else None,
                "mtime": st.st_mtime,
            })
        return _dump({
            "gid": gid,
            "path": str(target.relative_to(base)),
            "entries": entries,
        })
    finally:
        db.close()


@mcp.tool()
def read_workspace_file(gid: int, path: str) -> str:
    """读取工作空间内文件内容（仅 UTF-8 文本，最大 2MB）。"""
    db = _get_db()
    try:
        error = _workspace_group_error(db, gid)
        if error:
            return _dump({"error": error})
        target, error = _resolve_workspace_path(gid, path)
        if error:
            return _dump({"error": error})
        if not target.exists() or not target.is_file():
            return _dump({"error": f"文件不存在: {path}"})
        if target.stat().st_size > 2 * 1024 * 1024:
            return _dump({"error": "文件过大（>2MB），不支持在线读取"})
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _dump({"error": "二进制文件不支持在线读取"})
        return _dump({"path": path, "content": content})
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def write_workspace_file(gid: int, path: str, content: str) -> str:
    """写入工作空间文件（不存在则创建，父目录自动创建；.py/.sh 自动加可执行权限）。"""
    db = _get_db()
    try:
        error = _workspace_group_error(db, gid)
        if error:
            return _dump({"error": error})
        target, error = _resolve_workspace_path(gid, path)
        if error:
            return _dump({"error": error})
        if target.exists() and target.is_dir():
            return _dump({"error": f"目标是目录，不能写入文件: {path}"})
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if target.suffix in (".py", ".sh", ".bash"):
            target.chmod(target.stat().st_mode | 0o111)
        return _dump({"path": path, "size": target.stat().st_size})
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def make_workspace_dir(gid: int, path: str) -> str:
    """在工作空间内创建目录（已存在时静默成功）。"""
    db = _get_db()
    try:
        error = _workspace_group_error(db, gid)
        if error:
            return _dump({"error": error})
        target, error = _resolve_workspace_path(gid, path)
        if error:
            return _dump({"error": error})
        target.mkdir(parents=True, exist_ok=True)
        return _dump({"path": path})
    finally:
        db.close()


@mcp.tool()
@mcp_auth.require_admin
def delete_workspace_path(gid: int, path: str) -> str:
    """删除工作空间内的文件或目录（目录递归删除，不能删除工作空间根目录）。"""
    db = _get_db()
    try:
        error = _workspace_group_error(db, gid)
        if error:
            return _dump({"error": error})
        target, error = _resolve_workspace_path(gid, path)
        if error:
            return _dump({"error": error})
        if not target.exists():
            return _dump({"error": f"路径不存在: {path}"})
        base = _workspace_dir(gid)
        if target == base:
            return _dump({"error": "不能删除工作空间根目录"})
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return _dump({"deleted": path})
    finally:
        db.close()


def run_mcp_server(host: str, port: int, path: str = "/mcp"):
    """在独立线程中启动 MCP 服务（带 Bearer token 认证）。"""
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    cfg = get_config()
    mcp.settings.streamable_http_path = path or cfg.mcp_path
    if cfg.mcp_allowed_hosts or cfg.mcp_allowed_origins:
        mcp.settings.transport_security = TransportSecuritySettings(
            allowed_hosts=cfg.mcp_allowed_hosts,
            allowed_origins=cfg.mcp_allowed_origins,
        )

    store, info = mcp_auth.load_token_store(cfg)
    app = mcp.streamable_http_app()
    app = mcp_auth.MCPAuthMiddleware(
        app, store, allow_unauthenticated=cfg.mcp_allow_unauthenticated)

    if cfg.mcp_allow_unauthenticated:
        print("[ebaseline][WARN] MCP 认证已关闭（mcp.allow_unauthenticated=true）")
    elif info.get("generated"):
        print(f"[ebaseline] MCP 访问 token 已生成: {info.get('path')}（权限 0600）")
        print("[ebaseline] 客户端需设置 Authorization: Bearer <该文件内容>")
    elif info.get("path"):
        print(f"[ebaseline] MCP 使用已有 token: {info['path']}")

    uvicorn.run(app, host=host, port=port, log_level="info")


def start_mcp_background(host: str, port: int, path: str = "/mcp"):
    """在后台线程启动 MCP 服务。"""
    t = threading.Thread(target=run_mcp_server, args=(host, port, path), daemon=True)
    t.start()
    return t
