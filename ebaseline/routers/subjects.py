"""待测主体路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..auth import get_current_user, require_privilege
from ..database import get_db

router = APIRouter(prefix="/api/v1/subjects", tags=["subjects"])


@router.post("", response_model=schemas.SubjectResponse, status_code=201)
def create_subject(
    data: schemas.SubjectCreate,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(1)),
):
    """注册待测 agent。"""
    return crud.create_subject(db, data)


@router.get("", response_model=list[schemas.SubjectResponse])
def list_subjects(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """列出所有待测 agent。"""
    return crud.list_subjects(db)


@router.get("/{sid}", response_model=schemas.SubjectResponse)
def get_subject(
    sid: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """获取待测 agent 详情。"""
    subject = crud.get_subject(db, sid)
    if subject is None:
        raise HTTPException(status_code=404, detail="待测主体不存在")
    return subject


@router.put("/{sid}", response_model=schemas.SubjectResponse)
def update_subject(
    sid: int,
    data: schemas.SubjectUpdate,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(1)),
):
    """更新待测 agent。"""
    subject = crud.update_subject(db, sid, data)
    if subject is None:
        raise HTTPException(status_code=404, detail="待测主体不存在")
    return subject


@router.delete("/{sid}", status_code=204)
def delete_subject(
    sid: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(1)),
):
    """删除待测 agent。"""
    if not crud.delete_subject(db, sid):
        raise HTTPException(status_code=404, detail="待测主体不存在")
