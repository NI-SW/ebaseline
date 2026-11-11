"""用户管理路由：自助账户 + 管理员用户管理。"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..auth import (
    generate_api_key,
    get_current_user,
    hash_password,
    require_privilege,
    verify_password,
)
from ..database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["users"])


def _clear_api_key(user: models.User) -> None:
    user.api_key_hash = None
    user.api_key_prefix = None
    user.api_key_created_at = None
    user.api_key_last_used_at = None


# ── 自助账户（所有登录用户）───────────────────────────────────

@router.put("/me/profile", response_model=schemas.UserResponse)
def update_my_profile(
    data: schemas.ProfileUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """修改自己的用户名/邮箱；必须验证当前密码。"""
    if not verify_password(data.current_password, user.password):
        raise HTTPException(status_code=400, detail="当前密码错误")

    fields = data.model_fields_set
    if data.username is not None:
        new_name = data.username.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="用户名不能为空")
        if new_name != user.username:
            exists = (
                db.query(models.User)
                .filter(models.User.username == new_name,
                        models.User.id != user.id)
                .first()
            )
            if exists:
                raise HTTPException(status_code=409, detail="用户名已存在")
            user.username = new_name
    if "email" in fields:
        user.email = (data.email or "").strip() or None

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="用户名已存在")
    db.refresh(user)
    return user


@router.put("/me/password")
def change_my_password(
    data: schemas.PasswordChange,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """修改自己的密码；必须验证当前密码。"""
    if not verify_password(data.current_password, user.password):
        raise HTTPException(status_code=400, detail="当前密码错误")
    if data.current_password == data.new_password:
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")
    user.password = hash_password(data.new_password)
    db.commit()
    logger.info("用户 %s 修改了密码", user.username)
    return {"message": "密码已修改，下次登录请使用新密码"}


@router.post("/me/api-key", response_model=schemas.ApiKeyResponse)
def create_my_api_key(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """生成/重新生成个人 MCP API Key。明文只在本响应返回一次。"""
    had_key = bool(user.api_key_hash)
    raw, hashed, prefix = generate_api_key()
    user.api_key_hash = hashed
    user.api_key_prefix = prefix
    user.api_key_created_at = datetime.utcnow()
    user.api_key_last_used_at = None
    db.commit()
    db.refresh(user)
    logger.info("用户 %s 生成个人 API Key (prefix=%s)", user.username, prefix)
    message = (
        "API Key 已重新生成，旧 Key 已立即失效；请保存新 Key"
        if had_key else
        "API Key 已生成，请立即保存；关闭弹窗后无法再次查看"
    )
    return schemas.ApiKeyResponse(
        api_key=raw,
        prefix=prefix,
        created_at=user.api_key_created_at,
        message=message,
    )


@router.delete("/me/api-key")
def revoke_my_api_key(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """撤销自己的个人 API Key。"""
    _clear_api_key(user)
    db.commit()
    logger.info("用户 %s 撤销了个人 API Key", user.username)
    return {"message": "API Key 已撤销"}


# ── 管理员用户管理（需 privilege >= 1）────────────────────────


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
    actor: models.User = Depends(require_privilege(1)),
):
    """修改用户权限。

    等级 1 只能管理普通用户/管理员（0/1），不能触碰超管、不能授予超管；
    等级 2 可管理 0..2，但不能降级最后一个超管。
    """
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    if actor.privilege < 2:
        if user.privilege >= 2:
            raise HTTPException(status_code=403, detail="无权修改超级管理员")
        if data.privilege >= 2:
            raise HTTPException(status_code=403, detail="只有超级管理员可以授予超管权限")

    if user.privilege == 2 and data.privilege < 2:
        superadmins = db.query(models.User).filter(models.User.privilege == 2).count()
        if superadmins <= 1:
            raise HTTPException(status_code=400, detail="不能降级最后一个超级管理员")

    old = user.privilege
    user.privilege = data.privilege
    db.commit()
    db.refresh(user)
    logger.info(
        "权限变更: actor=%s(%s) target=%s(%s) %s -> %s",
        actor.username, actor.privilege, user.username, user.id, old, data.privilege)
    return user


@router.delete("/{user_id}/api-key")
def revoke_user_api_key(
    user_id: int,
    db: Session = Depends(get_db),
    actor: models.User = Depends(require_privilege(1)),
):
    """管理员撤销指定用户的 API Key。

    等级 1 只能处理非超管用户；等级 2 可处理所有用户。
    """
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if actor.privilege < 2 and user.privilege >= 2:
        raise HTTPException(status_code=403, detail="无权撤销超级管理员的 API Key")
    _clear_api_key(user)
    db.commit()
    logger.info(
        "API Key 撤销: actor=%s(%s) target=%s(%s)",
        actor.username, actor.privilege, user.username, user.id)
    return {"user_id": user.id, "message": "API Key 已撤销"}
