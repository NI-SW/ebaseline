"""任务路由。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..auth import get_current_user, require_privilege
from ..database import get_db

router = APIRouter(tags=["tasks"])


def _task_to_response(task: models.TaskList) -> schemas.TaskResponse:
    """将 ORM 对象转为响应模型，解析 JSON 字段。"""
    depends = None
    if task.depends:
        depends = [int(x.strip()) for x in task.depends.split(",") if x.strip()]
    input_data = None
    if task.input:
        try:
            input_data = json.loads(task.input)
        except json.JSONDecodeError:
            input_data = {"prompt": task.input}
    return schemas.TaskResponse(
        tid=task.tid,
        gid=task.gid,
        name=task.name,
        description=task.description,
        depends=depends,
        pre_script=task.pre_script,
        input=input_data,
        expect=task.expect,
        clean=task.clean,
    )


@router.post("/api/v1/groups/{gid}/tasks", response_model=schemas.TaskResponse, status_code=201)
def create_task(
    gid: int,
    data: schemas.TaskCreate,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(1)),
):
    """在组内创建任务（需 privilege >= 1）。"""
    if crud.get_task_group(db, gid) is None:
        raise HTTPException(status_code=404, detail="任务组不存在")
    task = crud.create_task(db, gid, data)
    return _task_to_response(task)


@router.get("/api/v1/groups/{gid}/tasks", response_model=list[schemas.TaskResponse])
def list_tasks_in_group(
    gid: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """列出组内任务。"""
    if crud.get_task_group(db, gid) is None:
        raise HTTPException(status_code=404, detail="任务组不存在")
    tasks = crud.list_tasks(db, gid=gid)
    return [_task_to_response(t) for t in tasks]


@router.get("/api/v1/tasks/{tid}", response_model=schemas.TaskResponse)
def get_task(
    tid: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """获取任务详情。"""
    task = crud.get_task(db, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _task_to_response(task)


@router.put("/api/v1/tasks/{tid}", response_model=schemas.TaskResponse)
def update_task(
    tid: int,
    data: schemas.TaskUpdate,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(1)),
):
    """更新任务。"""
    task = crud.update_task(db, tid, data)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _task_to_response(task)


@router.delete("/api/v1/tasks/{tid}", status_code=204)
def delete_task(
    tid: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(1)),
):
    """删除任务。"""
    if not crud.delete_task(db, tid):
        raise HTTPException(status_code=404, detail="任务不存在")
