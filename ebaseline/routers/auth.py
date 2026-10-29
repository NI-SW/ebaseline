"""认证路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..auth import (
    create_access_token,
    get_current_user,
    hash_password,
    init_admin_user,
    verify_password,
)
from ..database import get_db

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/register", response_model=schemas.UserResponse, status_code=201)
def register(data: schemas.RegisterRequest, db: Session = Depends(get_db)):
    """注册新用户（默认 privilege=0）。"""
    existing = db.query(models.User).filter(models.User.username == data.username).first()
    if existing:
        raise HTTPException(status_code=409, detail="用户名已存在")
    user = models.User(
        username=data.username,
        password=hash_password(data.password),
        email=data.email,
        privilege=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=schemas.TokenResponse)
def login(data: schemas.LoginRequest, db: Session = Depends(get_db)):
    """登录，返回 JWT token。"""
    user = db.query(models.User).filter(models.User.username == data.username).first()
    if user is None or not verify_password(data.password, user.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )
    token = create_access_token(user.id, user.username)
    from ..config import get_config
    cfg = get_config()
    return schemas.TokenResponse(
        access_token=token,
        expires_in=cfg.auth_token_expire_minutes * 60,
    )


@router.get("/me", response_model=schemas.UserResponse)
def me(user: models.User = Depends(get_current_user)):
    """获取当前用户信息。"""
    return user


@router.post("/init-admin", response_model=schemas.UserResponse)
def init_admin(db: Session = Depends(get_db)):
    """初始化 admin 用户（如果不存在）。无需登录，仅首次使用。"""
    return init_admin_user(db)
