"""批次路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud, executor, models, schemas
from ..auth import get_current_user, require_privilege
from ..database import get_db

router = APIRouter(prefix="/api/v1/batches", tags=["batches"])

_STATUS_TEXT = {
    0: "未执行",
    1: "执行中",
    2: "已完成",
    3: "执行失败",
    4: "部分成功",
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
    """列出所有批次。"""
    return crud.list_batches(db, skip, limit)


@router.get("/{batch_id}", response_model=schemas.BatchDetailResponse)
def get_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """获取批次详情（含每个任务的执行状态）。"""
    batch = crud.get_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")

    tasks = crud.list_tasks(db, gid=batch.gid)
    task_details = []
    for task in tasks:
        result = crud.get_task_result(db, batch.sid, batch.gid, task.tid)
        task_details.append({
            "tid": task.tid,
            "name": task.name,
            "result": result.result if result else 0,
            "result_text": {0: "未执行", 1: "成功", 2: "失败"}.get(result.result if result else 0, "未知"),
            "process": result.process if result else None,
            "create_time": result.create_time.isoformat() if result else None,
        })

    return schemas.BatchDetailResponse(
        id=batch.id,
        sid=batch.sid,
        gid=batch.gid,
        status=batch.status,
        create_time=batch.create_time,
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
    """删除批次（级联删除结果）。"""
    if not crud.delete_batch(db, batch_id):
        raise HTTPException(status_code=404, detail="批次不存在")
