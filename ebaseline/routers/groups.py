"""任务组路由。"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud, models, runtime, schemas
from ..auth import get_current_user, require_privilege
from ..config import get_config
from ..database import get_db
from ..security import mask_variables
from .workspace import delete_workspace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/groups", tags=["groups"])


def _group_to_response(group: models.TaskGroup) -> schemas.TaskGroupResponse:
    """将 ORM 对象转为响应模型，解析 variables JSON 字符串。"""
    variables = None
    if group.variables:
        try:
            variables = json.loads(group.variables)
        except json.JSONDecodeError:
            variables = None
    cfg = get_config()
    if variables is not None and cfg.security_mask_group_secrets:
        variables = mask_variables(variables, cfg.security_mask_placeholder)
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
    """删除任务组（级联删除组内任务、批次与结果，并清理工作空间与运行上下文）。"""
    if not crud.delete_task_group(db, gid):
        raise HTTPException(status_code=404, detail="任务组不存在")
    # 数据库记录已提交，目录清理失败不影响删除结果，仅记录日志便于手工处理
    try:
        delete_workspace(gid)
    except OSError:
        logger.warning("删除任务组 %s 的工作空间目录失败", gid, exc_info=True)
    try:
        runtime.purge_group(gid)
    except OSError:
        logger.warning("删除任务组 %s 的运行上下文失败", gid, exc_info=True)
