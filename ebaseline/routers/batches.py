"""批次路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud, executor, models, schemas
from ..auth import get_current_user, require_privilege
from ..database import get_db
from ..metrics import compute_batch_metrics, pending_summary, result_summary

router = APIRouter(prefix="/api/v1/batches", tags=["batches"])

_STATUS_TEXT = {
    0: "未执行",
    1: "执行中",
    2: "已完成",
    3: "执行失败",
    4: "部分成功",
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
    return {
        b.id: compute_batch_metrics(
            by_batch.get(b.id, []),
            task_count=task_counts.get(b.gid, 0),
        )
        for b in batches
    }


def _batch_dict(batch: models.TaskBatch, metrics: dict | None = None) -> dict:
    return {
        "id": batch.id,
        "sid": batch.sid,
        "gid": batch.gid,
        "status": batch.status,
        "create_time": batch.create_time,
        "metrics": metrics or {},
    }


@router.post("", response_model=schemas.BatchResponse, status_code=201)
def create_batch(
    data: schemas.BatchCreate,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """创建批次。"""
    if crud.get_subject(db, data.sid) is None:
        raise HTTPException(status_code=404, detail=f"待测主体 {data.sid} 不存在")
    if crud.get_task_group(db, data.gid) is None:
        raise HTTPException(status_code=404, detail=f"任务组 {data.gid} 不存在")
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
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """获取批次详情（聚合指标 + 每个任务的执行结果）。"""
    batch = crud.get_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")

    subject = crud.get_subject(db, batch.sid)
    group = crud.get_task_group(db, batch.gid)
    tasks = crud.list_tasks(db, gid=batch.gid)
    results = crud.list_results(db, batch_id=batch.id)
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
            "result": r.result if r else 0,
            "result_detail": r.result_detail if r else None,
            "verdict": summary["verdict"],
            "verdict_text": summary["verdict_text"],
            "result_text": summary["verdict_text"],
            "process": r.process if r else None,
            "create_time": r.create_time.isoformat() if r else None,
            "result_id": r.id if r else None,
            "metrics": summary,
        })

    return schemas.BatchDetailResponse(
        id=batch.id,
        sid=batch.sid,
        gid=batch.gid,
        status=batch.status,
        create_time=batch.create_time,
        subject_name=subject.name if subject else None,
        group_name=group.name if group else None,
        metrics=compute_batch_metrics(results, task_count=len(tasks)),
        tasks=task_details,
    )


@router.post("/{batch_id}/run")
def run_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """异步执行批次。"""
    batch = crud.get_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    if batch.status == 1:
        raise HTTPException(status_code=409, detail="批次正在执行中")
    if not executor.start_batch_async(batch_id):
        raise HTTPException(status_code=409, detail="批次已在执行队列中")
    return {"batch_id": batch_id, "status": 1, "message": "批次已启动"}


@router.post("/{batch_id}/cancel")
def cancel_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """取消正在执行的批次。"""
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
