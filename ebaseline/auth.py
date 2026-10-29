"""认证与权限：JWT + bcrypt。"""
from __future__ import annotations

from datetime import datetime, timedelta

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from .config import get_config
from .database import get_db
from . import models

security = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    """bcrypt 散列密码。"""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验密码。"""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_access_token(user_id: int, username: str) -> str:
    cfg = get_config()
    expire = datetime.utcnow() + timedelta(minutes=cfg.auth_token_expire_minutes)
    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": expire,
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, cfg.auth_secret_key, algorithm=cfg.auth_algorithm)


def decode_token(token: str) -> dict:
    cfg = get_config()
    try:
        return jwt.decode(token, cfg.auth_secret_key, algorithms=[cfg.auth_algorithm])
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"无效或已过期的 token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> models.User:
    """FastAPI 依赖：获取当前登录用户。未登录抛 401。"""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(credentials.credentials)
    user_id = int(payload.get("sub", 0))
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_privilege(min_level: int):
    """FastAPI 依赖工厂：要求最低权限等级。

    用法:
        @router.get("/admin", dependencies=[Depends(require_privilege(1))])
    """
    def checker(user: models.User = Depends(get_current_user)) -> models.User:
        if user.privilege < min_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要权限等级 >= {min_level}，当前为 {user.privilege}",
            )
        return user
    return checker


def init_admin_user(db: Session) -> models.User:
    """确保 admin 用户存在（首次启动时调用）。"""
    admin = db.query(models.User).filter(models.User.username == "admin").first()
    if admin is None:
        admin = models.User(
            username="admin",
            password=hash_password("admin123"),
            email=None,
            privilege=2,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
    return admin
