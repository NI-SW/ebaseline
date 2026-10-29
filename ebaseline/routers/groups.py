"""任务组路由。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..auth import get_current_user, require_privilege
from ..database import get_db

router = APIRouter(prefix="/api/v1/groups", tags=["groups"])


def _group_to_response(group: models.TaskGroup) -> schemas.TaskGroupResponse:
    """将 ORM 对象转为响应模型，解析 variables JSON 字符串。"""
    variables = None
    if group.variables:
        try:
            variables = json.loads(group.variables)
        except json.JSONDecodeError:
            variables = None
    return schemas.TaskGroupResponse(
        id=group.id,
        name=group.name,
        description=group.description,
        variables=variables,
        python_version=group.python_version,
    )


@router.post("", response_model=schemas.TaskGroupResponse, status_code=201)
def create_group(
    data: schemas.TaskGroupCreate,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(1)),
):
    """创建任务组（需 privilege >= 1）。"""
    return _group_to_response(crud.create_task_group(db, data))


@router.get("", response_model=list[schemas.TaskGroupResponse])
def list_groups(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """列出所有任务组。"""
    return [_group_to_response(g) for g in crud.list_task_groups(db, skip, limit)]


@router.get("/{gid}", response_model=schemas.TaskGroupResponse)
def get_group(
    gid: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """获取任务组详情。"""
    group = crud.get_task_group(db, gid)
    if group is None:
        raise HTTPException(status_code=404, detail="任务组不存在")
    return _group_to_response(group)


@router.put("/{gid}", response_model=schemas.TaskGroupResponse)
def update_group(
    gid: int,
    data: schemas.TaskGroupUpdate,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(1)),
):
    """更新任务组。"""
    group = crud.update_task_group(db, gid, data)
    if group is None:
        raise HTTPException(status_code=404, detail="任务组不存在")
    return _group_to_response(group)


@router.delete("/{gid}", status_code=204)
def delete_group(
    gid: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(1)),
):
    """删除任务组（级联删除组内任务）。"""
    if not crud.delete_task_group(db, gid):
        raise HTTPException(status_code=404, detail="任务组不存在")
