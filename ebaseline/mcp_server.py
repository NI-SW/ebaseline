"""MCP 服务：为智能体暴露测试任务操作工具集。

使用官方 mcp SDK 的 FastMCP，支持 streamable HTTP。
智能体可通过 MCP 客户端连接本服务，执行：
  - 浏览任务组/任务
  - 创建/启动批次
  - 上报任务结果
  - 查询批次状态/结果
"""
from __future__ import annotations

import json
import threading
from typing import Any

from mcp.server import FastMCP
from sqlalchemy.orm import Session

from . import crud, executor, models
from .database import _get_session_factory
from .executor import render_variables

mcp = FastMCP("ebaseline", stateless_http=True)


def _get_db() -> Session:
    """获取数据库会话（每次调用新建，避免线程安全问题）。"""
    return _get_session_factory()()


def _task_to_dict(task: models.TaskList, db: Session | None = None) -> dict:
    depends = None
    if task.depends:
        depends = [int(x.strip()) for x in task.depends.split(",") if x.strip()]
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
        "expect": task.expect,
        "clean": task.clean,
    }


@mcp.tool()
def list_task_groups() -> str:
    """列出所有任务组。返回 JSON 数组，每个元素含 id/name/description。"""
    db = _get_db()
    try:
        groups = crud.list_task_groups(db)
        return json.dumps([
            {"id": g.id, "name": g.name, "description": g.description}
            for g in groups
        ], ensure_ascii=False, indent=2)
    finally:
        db.close()


@mcp.tool()
def get_task_group(gid: int) -> str:
    """获取任务组详情（含 variables 变量定义）。"""
    db = _get_db()
    try:
        group = crud.get_task_group(db, gid)
        if group is None:
            return json.dumps({"error": f"任务组 {gid} 不存在"}, ensure_ascii=False)
        variables = None
        if group.variables:
            try:
                variables = json.loads(group.variables)
            except json.JSONDecodeError:
                variables = group.variables
        return json.dumps({
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "variables": variables,
        }, ensure_ascii=False, indent=2)
    finally:
        db.close()


@mcp.tool()
def list_tasks_in_group(gid: int) -> str:
    """列出指定任务组内的所有任务。"""
    db = _get_db()
    try:
        if crud.get_task_group(db, gid) is None:
            return json.dumps({"error": f"任务组 {gid} 不存在"}, ensure_ascii=False)
        tasks = crud.list_tasks(db, gid=gid)
        return json.dumps([
            {"tid": t.tid, "gid": t.gid, "name": t.name, "description": t.description}
            for t in tasks
        ], ensure_ascii=False, indent=2)
    finally:
        db.close()


@mcp.tool()
def get_task_detail(tid: int) -> str:
    """获取任务完整定义（含 prompt 模板、依赖、脚本配置等）。"""
    db = _get_db()
    try:
        task = crud.get_task(db, tid)
        if task is None:
            return json.dumps({"error": f"任务 {tid} 不存在"}, ensure_ascii=False)
        return json.dumps(_task_to_dict(task), ensure_ascii=False, indent=2)
    finally:
        db.close()


@mcp.tool()
def render_task_prompt(tid: int) -> str:
    """用任务组变量渲染任务 prompt，返回可直接发给 Agent 的最终文本。"""
    db = _get_db()
    try:
        task = crud.get_task(db, tid)
        if task is None:
            return json.dumps({"error": f"任务 {tid} 不存在"}, ensure_ascii=False)
        group = crud.get_task_group(db, task.gid)
        if group is None:
            return json.dumps({"error": f"任务组 {task.gid} 不存在"}, ensure_ascii=False)

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
        return json.dumps({
            "tid": tid,
            "prompt": prompt,
            "conversation": input_data.get("conversation", []),
        }, ensure_ascii=False, indent=2)
    finally:
        db.close()


@mcp.tool()
def list_subjects() -> str:
    """列出所有待测 agent。"""
    db = _get_db()
    try:
        subjects = crud.list_subjects(db)
        return json.dumps([
            {"id": s.id, "name": s.name, "ipaddr": s.ipaddr, "port": s.port, "api_mode": s.api_mode}
            for s in subjects
        ], ensure_ascii=False, indent=2)
    finally:
        db.close()


@mcp.tool()
def create_batch(sid: int, gid: int) -> str:
    """创建测试批次。返回批次 id，可用于后续启动和查询。"""
    db = _get_db()
    try:
        if crud.get_subject(db, sid) is None:
            return json.dumps({"error": f"待测主体 {sid} 不存在"}, ensure_ascii=False)
        if crud.get_task_group(db, gid) is None:
            return json.dumps({"error": f"任务组 {gid} 不存在"}, ensure_ascii=False)
        batch = crud.create_batch(db, sid, gid)
        return json.dumps({
            "batch_id": batch.id,
            "sid": batch.sid,
            "gid": batch.gid,
            "status": batch.status,
        }, ensure_ascii=False, indent=2)
    finally:
        db.close()


@mcp.tool()
def start_batch(batch_id: int) -> str:
    """启动批次执行（异步，立即返回）。批次将在后台线程中执行所有任务。"""
    db = _get_db()
    try:
        batch = crud.get_batch(db, batch_id)
        if batch is None:
            return json.dumps({"error": f"批次 {batch_id} 不存在"}, ensure_ascii=False)
        if batch.status == 1:
            return json.dumps({"error": "批次已在执行中"}, ensure_ascii=False)
        if not executor.start_batch_async(batch_id):
            return json.dumps({"error": "批次已在执行队列中"}, ensure_ascii=False)
        return json.dumps({
            "batch_id": batch_id,
            "status": 1,
            "message": "批次已启动",
        }, ensure_ascii=False, indent=2)
    finally:
        db.close()


@mcp.tool()
def get_batch_status(batch_id: int) -> str:
    """查询批次状态。status: 0=未执行, 1=执行中, 2=已完成, 3=执行失败, 4=部分成功。"""
    db = _get_db()
    try:
        batch = crud.get_batch(db, batch_id)
        if batch is None:
            return json.dumps({"error": f"批次 {batch_id} 不存在"}, ensure_ascii=False)
        status_text = {
            0: "未执行", 1: "执行中", 2: "已完成", 3: "执行失败", 4: "部分成功",
        }.get(batch.status, "未知")
        return json.dumps({
            "batch_id": batch.id,
            "sid": batch.sid,
            "gid": batch.gid,
            "status": batch.status,
            "status_text": status_text,
            "create_time": batch.create_time.isoformat() if batch.create_time else None,
        }, ensure_ascii=False, indent=2)
    finally:
        db.close()


@mcp.tool()
def get_task_results(batch_id: int) -> str:
    """查询批次中所有任务的执行结果。"""
    db = _get_db()
    try:
        batch = crud.get_batch(db, batch_id)
        if batch is None:
            return json.dumps({"error": f"批次 {batch_id} 不存在"}, ensure_ascii=False)
        tasks = crud.list_tasks(db, gid=batch.gid)
        results = []
        for task in tasks:
            r = crud.get_task_result(db, batch.sid, batch.gid, task.tid)
            results.append({
                "tid": task.tid,
                "name": task.name,
                "result": r.result if r else 0,
                "result_text": {0: "未执行", 1: "成功", 2: "失败"}.get(r.result if r else 0, "未知"),
                "process": (r.process or "")[:2000] if r else None,
                "create_time": r.create_time.isoformat() if r else None,
            })
        return json.dumps(results, ensure_ascii=False, indent=2)
    finally:
        db.close()


@mcp.tool()
def submit_task_result(batch_id: int, tid: int, result: int, process: str | None = None) -> str:
    """智能体主动上报任务结果。result: 0=未执行, 1=成功, 2=失败。

    用于智能体主导执行模式：智能体通过 MCP 拉取任务、自主执行、上报结果。
    """
    db = _get_db()
    try:
        batch = crud.get_batch(db, batch_id)
        if batch is None:
            return json.dumps({"error": f"批次 {batch_id} 不存在"}, ensure_ascii=False)
        task = crud.get_task(db, tid)
        if task is None:
            return json.dumps({"error": f"任务 {tid} 不存在"}, ensure_ascii=False)
        if task.gid != batch.gid:
            return json.dumps({"error": f"任务 {tid} 不属于批次 {batch_id} 的任务组"}, ensure_ascii=False)

        r = crud.create_result(
            db,
            sid=batch.sid,
            gid=batch.gid,
            tid=tid,
            result=result,
            process=process,
        )
        return json.dumps({
            "result_id": r.id,
            "batch_id": batch_id,
            "tid": tid,
            "result": result,
            "message": "结果已记录",
        }, ensure_ascii=False, indent=2)
    finally:
        db.close()


@mcp.tool()
def export_batch_csv(batch_id: int) -> str:
    """导出批次结果为 CSV 格式文本。"""
    db = _get_db()
    try:
        batch = crud.get_batch(db, batch_id)
        if batch is None:
            return json.dumps({"error": f"批次 {batch_id} 不存在"}, ensure_ascii=False)
        results = crud.list_results(db, sid=batch.sid, gid=batch.gid)
        if not results:
            return json.dumps({"error": "批次无结果"}, ensure_ascii=False)

        import csv
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "sid", "gid", "tid", "result", "process", "create_time"])
        for r in results:
            writer.writerow([
                r.id, r.sid, r.gid, r.tid, r.result,
                (r.process or "").replace("\n", "\\n"),
                r.create_time.isoformat() if r.create_time else "",
            ])
        return output.getvalue()
    finally:
        db.close()


def run_mcp_server(host: str, port: int, path: str = "/mcp"):
    """在独立线程中启动 MCP 服务。"""
    import uvicorn

    # FastMCP 的 streamable HTTP app 默认挂 /mcp
    app = mcp.streamable_http_app()
    uvicorn.run(app, host=host, port=port, log_level="info")


def start_mcp_background(host: str, port: int, path: str = "/mcp"):
    """在后台线程启动 MCP 服务。"""
    t = threading.Thread(target=run_mcp_server, args=(host, port, path), daemon=True)
    t.start()
    return t
