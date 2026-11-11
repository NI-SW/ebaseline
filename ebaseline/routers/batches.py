"""批次路由：创建、执行、取消、attempt 历史与预检。"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud, executor, manifest as manifest_mod, models, schemas, task_graph
from ..auth import get_current_user, require_privilege
from ..config import get_config
from ..database import get_db
from ..metrics import compute_batch_metrics, pending_summary, result_summary

router = APIRouter(prefix="/api/v1/batches", tags=["batches"])

_STATUS_TEXT = {
    0: "未执行",
    1: "执行中",
    2: "已完成",
    3: "执行失败",
    4: "部分成功",
    5: "已取消",
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
        # 有 attempt 的批次只统计最新一轮，避免旧轮结果混入
        if b.attempt and b.attempt > 0:
            rows = [r for r in rows if r.attempt == b.attempt]
        out[b.id] = compute_batch_metrics(
            rows, task_count=task_counts.get(b.gid, 0))
    return out


def _parse_depends(depends_str: str | None) -> list[int]:
    try:
        return task_graph.parse_depends(depends_str)
    except task_graph.DependencyError:
        return []


def _parse_input(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {"prompt": raw}


def _manifest_ok(manifest: dict) -> bool:
    return bool(
        manifest
        and manifest.get("manifest_version") == manifest_mod.MANIFEST_VERSION
        and not manifest.get("error")
    )


def _batch_tasks(db: Session, batch: models.TaskBatch):
    """返回 (tasks, from_manifest, manifest)。

    有合法 manifest 时使用冻结定义（即使任务后来被编辑/删除）；
    旧批次没有 manifest 时回退到当前任务。
    """
    manifest = manifest_mod.parse_manifest(batch.manifest)
    if _manifest_ok(manifest):
        return manifest_mod.tasks_from_manifest(manifest), True, manifest
    return crud.list_tasks(db, gid=batch.gid), False, manifest


def _results_for_attempt(db: Session, batch: models.TaskBatch,
                         attempt: Optional[int]) -> list[models.TaskResult]:
    if attempt is not None:
        # attempt=0 时 crud 会同时匹配 NULL（旧结果）
        return crud.list_results(db, batch_id=batch.id, attempt=attempt)
    effective = batch.attempt
    if effective and effective > 0:
        return crud.list_results(db, batch_id=batch.id, attempt=effective)
    # 批次还没跑过 attempt：显示该批次的全部结果
    return crud.list_results(db, batch_id=batch.id)


def _script_missing(manifest: dict, tid: int) -> list[str]:
    missing: list[str] = []
    for item in manifest.get("tasks") or []:
        if not isinstance(item, dict) or int(item.get("tid", -1)) != int(tid):
            continue
        for field, script in (item.get("scripts") or {}).items():
            if isinstance(script, dict) and script.get("missing"):
                missing.append(field)
    return missing


def _batch_dict(batch: models.TaskBatch, metrics: dict | None = None) -> dict:
    return {
        "id": batch.id,
        "sid": batch.sid,
        "gid": batch.gid,
        "status": batch.status,
        "attempt": batch.attempt or 0,
        "create_time": batch.create_time,
        "metrics": metrics or {},
    }


def _available_attempts(db: Session, batch: models.TaskBatch) -> list[int]:
    attempts = set(crud.list_batch_attempts(db, batch.id))
    if batch.attempt:
        attempts.add(int(batch.attempt))
    return sorted(attempts)


def _dependency_issues(tasks) -> list[str]:
    return task_graph.validate_tasks(tasks)


@router.post("", response_model=schemas.BatchResponse, status_code=201)
def create_batch(
    data: schemas.BatchCreate,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """创建批次（同时冻结任务/变量/脚本 hash 清单）。"""
    if crud.get_subject(db, data.sid) is None:
        raise HTTPException(status_code=404, detail=f"待测主体 {data.sid} 不存在")
    if crud.get_task_group(db, data.gid) is None:
        raise HTTPException(status_code=404, detail=f"任务组 {data.gid} 不存在")
    tasks = crud.list_tasks(db, gid=data.gid)
    issues = _dependency_issues(tasks)
    if issues:
        raise HTTPException(
            status_code=400, detail="依赖图不合法：" + "；".join(issues))
    return crud.create_batch(db, data.sid, data.gid)


@router.get("", response_model=list[schemas.BatchResponse])
def list_batches(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """列出所有批次（含聚合指标）。"""
    batches = crud.list_batches(db, skip, limit)
    metrics_map = _batch_metrics_map(db, batches)
    return [_batch_dict(b, metrics_map[b.id]) for b in batches]


@router.get("/{batch_id}", response_model=schemas.BatchDetailResponse)
def get_batch(
    batch_id: int,
    attempt: Optional[int] = None,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """获取批次详情；attempt 指定时查看该轮历史结果。"""
    batch = crud.get_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")

    subject = crud.get_subject(db, batch.sid)
    group = crud.get_task_group(db, batch.gid)
    tasks, from_manifest, manifest = _batch_tasks(db, batch)
    results = _results_for_attempt(db, batch, attempt)
    # list_results 按 id 倒序，每 tid 取最新一条
    by_tid: dict[int, models.TaskResult] = {}
    for r in results:
        by_tid.setdefault(r.tid, r)

    task_details = []
    for task in tasks:
        r = by_tid.get(task.tid)
        summary = result_summary(r) if r else pending_summary()
        task_details.append({
            "tid": task.tid,
            "name": task.name,
            "depends": _parse_depends(task.depends),
            "pre_script": task.pre_script,
            "expect": task.expect,
            "clean": task.clean,
            "timeout": task.timeout,
            "input": _parse_input(task.input),
            "result": r.result if r else 0,
            "result_detail": r.result_detail if r else None,
            "attempt": r.attempt if r else (batch.attempt or 0),
            "verdict": summary["verdict"],
            "verdict_text": summary["verdict_text"],
            "result_text": summary["verdict_text"],
            "process": r.process if r else None,
            "create_time": r.create_time.isoformat() if r else None,
            "result_id": r.id if r else None,
            "metrics": summary,
            "definition_source": "manifest" if from_manifest else "live",
            "scripts_missing": _script_missing(manifest, task.tid),
        })

    cfg = get_config()
    return schemas.BatchDetailResponse(
        id=batch.id,
        sid=batch.sid,
        gid=batch.gid,
        status=batch.status,
        attempt=batch.attempt or 0,
        create_time=batch.create_time,
        subject_name=subject.name if subject else None,
        group_name=group.name if group else None,
        metrics=compute_batch_metrics(results, task_count=len(tasks)),
        tasks=task_details,
        running_task_tid=executor.running_task_tid(batch.id),
        manifest_summary=manifest_mod.manifest_summary(
            manifest, cfg.security_mask_placeholder) if from_manifest else None,
        available_attempts=_available_attempts(db, batch),
        viewing_attempt=(batch.attempt if attempt is None else attempt),
    )


@router.get("/{batch_id}/precheck")
def precheck_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """执行前预检：依赖图、无校验任务、脚本缺失、清单状态。"""
    batch = crud.get_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    tasks, from_manifest, manifest = _batch_tasks(db, batch)
    issues = _dependency_issues(tasks)
    no_expect = [t.tid for t in tasks if not (getattr(t, "expect", None) or "").strip()]
    missing = []
    if from_manifest:
        missing = (manifest_mod.manifest_summary(manifest) or {}).get("scripts_missing", [])
    # 旧批次没有清单：只能展示当前定义，无法证明创建时脚本存在
    return {
        "batch_id": batch_id,
        "attempt": batch.attempt or 0,
        "from_manifest": from_manifest,
        "task_count": len(tasks),
        "dependency_issues": issues,
        "no_expect_tids": no_expect,
        "scripts_missing": missing,
        "ok": not issues and bool(tasks),
        "warnings": (
            ([f"{len(no_expect)} 个任务没有校验脚本，执行后只能记为未校验"]
             if no_expect else [])
            + ([f"{len(missing)} 个脚本在创建批次时不存在"] if missing else [])
        ),
    }


@router.get("/{batch_id}/attempts")
def list_batch_attempts(
    batch_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """列出批次每一轮 attempt 的时间范围与指标。"""
    batch = crud.get_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    tasks, _from_manifest, _manifest = _batch_tasks(db, batch)
    results = crud.list_results(db, batch_id=batch_id)
    grouped: dict[int, list[models.TaskResult]] = {}
    for r in results:
        key = int(r.attempt) if r.attempt is not None else 0
        grouped.setdefault(key, []).append(r)

    out = []
    for attempt_no in sorted(grouped):
        rows = grouped[attempt_no]
        times = [r.create_time for r in rows if r.create_time]
        out.append({
            "attempt": attempt_no,
            "result_count": len(rows),
            "first_time": min(times).isoformat() if times else None,
            "last_time": max(times).isoformat() if times else None,
            "metrics": compute_batch_metrics(rows, task_count=len(tasks)),
        })
    return {
        "batch_id": batch_id,
        "current_attempt": batch.attempt or 0,
        "attempts": out,
    }


@router.post("/{batch_id}/run")
def run_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """异步执行批次（执行前校验依赖图）。"""
    batch = crud.get_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    if batch.status == 1:
        raise HTTPException(status_code=409, detail="批次正在执行中")
    if executor.has_running_task(batch_id):
        raise HTTPException(status_code=409, detail="该批次有任务正在单独执行，请等待完成")

    tasks, _from_manifest, _manifest = _batch_tasks(db, batch)
    issues = _dependency_issues(tasks)
    if issues:
        raise HTTPException(
            status_code=400, detail="依赖图不合法，无法执行：" + "；".join(issues))
    if not tasks:
        raise HTTPException(status_code=400, detail="批次没有可执行的任务")

    if not executor.start_batch_async(batch_id):
        raise HTTPException(status_code=409, detail="批次已在执行队列中")
    return {"batch_id": batch_id, "status": 1, "message": "批次已启动"}


@router.post("/{batch_id}/tasks/{tid}/run")
def run_batch_task(
    batch_id: int,
    tid: int,
    force: bool = False,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """异步执行批次内的单个任务。

    force=true 时忽略依赖检查（用于单独调试某个任务）。
    单任务执行使用当前任务定义，若与 manifest 不一致会在结果 metrics 标记。
    """
    batch = crud.get_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    if batch.status == 1 or executor.is_batch_running(batch_id):
        raise HTTPException(status_code=409, detail="批次正在执行中")
    if crud.get_subject(db, batch.sid) is None:
        raise HTTPException(status_code=404, detail="批次关联的待测主体不存在")
    task = crud.get_task(db, tid)
    if task is None or task.gid != batch.gid:
        raise HTTPException(status_code=404, detail=f"任务 {tid} 不存在或不属于该批次任务组")
    if executor.has_running_task(batch_id):
        raise HTTPException(status_code=409, detail="该批次有其他任务正在执行，请等待完成")
    if not executor.start_task_async(batch_id, tid, force):
        raise HTTPException(status_code=409, detail="任务已在执行队列中")
    return {
        "batch_id": batch_id,
        "tid": tid,
        "force": force,
        "message": "任务已启动",
    }


@router.post("/{batch_id}/cancel")
def cancel_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """取消正在执行的批次（同时中断正在运行的脚本/Agent 调用）。"""
    batch = crud.get_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    if not executor.cancel_batch(batch_id):
        raise HTTPException(status_code=409, detail="批次未在执行中")
    return {"batch_id": batch_id, "message": "已发送取消信号"}


@router.delete("/{batch_id}", status_code=204)
def delete_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(1)),
):
    """删除批次（只删除该批次自己的结果）。"""
    if not crud.delete_batch(db, batch_id):
        raise HTTPException(status_code=404, detail="批次不存在")
