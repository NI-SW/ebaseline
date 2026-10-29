"""用户管理路由（需 privilege >= 1）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..auth import require_privilege
from ..database import get_db

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.get("", response_model=list[schemas.UserResponse])
def list_users(
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(1)),
):
    """列出所有用户。"""
    return db.query(models.User).all()


@router.put("/{user_id}/privilege", response_model=schemas.UserResponse)
def update_privilege(
    user_id: int,
    data: schemas.UserPrivilegeUpdate,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(1)),
):
    """修改用户权限。"""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    user.privilege = data.privilege
    db.commit()
    db.refresh(user)
    return user
