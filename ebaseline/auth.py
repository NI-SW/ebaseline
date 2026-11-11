"""认证与权限：JWT + bcrypt。"""
from __future__ import annotations

import hashlib
import logging
import secrets
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
logger = logging.getLogger(__name__)


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


# ── 个人 MCP API Key ───────────────────────────────────────────
API_KEY_PREFIX = "ebk_"
API_KEY_DISPLAY_PREFIX_LEN = 10  # 例如 ebk_AbCdEf


def hash_api_key(raw: str) -> str:
    """API Key 只存 SHA-256，数据库泄露时无法直接还原明文。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """生成 (明文, SHA-256, 展示前缀)。明文只返回一次。"""
    raw = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return raw, hash_api_key(raw), raw[:API_KEY_DISPLAY_PREFIX_LEN]


def lookup_user_by_api_key(db: Session, raw: str) -> models.User | None:
    """按个人 API Key 查找用户；格式不匹配时不做数据库查询。"""
    if not raw or not raw.startswith(API_KEY_PREFIX):
        return None
    return (
        db.query(models.User)
        .filter(models.User.api_key_hash == hash_api_key(raw))
        .first()
    )


def touch_api_key_usage(db: Session, user: models.User,
                        min_interval_seconds: int = 60) -> None:
    """节流更新 Key 最近使用时间，避免每个 MCP 请求都写库。"""
    now = datetime.utcnow()
    last = user.api_key_last_used_at
    if last is None or (now - last).total_seconds() >= min_interval_seconds:
        user.api_key_last_used_at = now
        db.commit()


def init_admin_user(db: Session) -> models.User:
    """确保 admin 用户存在（首次启动时调用）。

    初始密码优先取 auth.initial_admin_password / EBASE_ADMIN_PASSWORD；
    都没有时随机生成，并在启动日志中提示一次（不再使用固定 admin123）。
    """
    admin = db.query(models.User).filter(models.User.username == "admin").first()
    if admin is None:
        cfg = get_config()
        password = cfg.auth_initial_admin_password
        generated = not password
        if generated:
            password = secrets.token_urlsafe(12)
        admin = models.User(
            username="admin",
            password=hash_password(password),
            email=None,
            privilege=2,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
        if generated:
            # 首次启动必须让运维拿到密码；只打印一次，登录后应立即修改
            message = (
                "已创建 admin 用户，随机初始密码（仅本次启动显示，请立即修改）: "
                f"{password}")
            logger.warning(message)
            print(f"[ebaseline] {message}", flush=True)
    return admin
