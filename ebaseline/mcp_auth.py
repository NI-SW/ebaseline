"""MCP 认证与工具级角色控制。

FastMCP 本身默认不带用户认证。这里在 streamable HTTP 应用外层加一个
纯 ASGI 中间件，要求每个请求携带 `Authorization: Bearer <token>`，
在请求进入任何工具前完成校验；工具再用 `require_admin` 区分只读/管理权限。

角色：
- admin：全部工具；
- readonly：list/get/export 等只读工具，写操作和敏感渲染被拒绝。
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import hmac
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Callable, Optional

from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import API_KEY_PREFIX

logger = logging.getLogger(__name__)

ROLE_ADMIN = "admin"
ROLE_READONLY = "readonly"

_role: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "ebaseline_mcp_role", default=None)


class TokenStore:
    """静态 token → 角色映射（常量时间比较）。"""

    def __init__(self, admin_token: str = "", readonly_token: str = ""):
        self.admin_token = (admin_token or "").strip()
        self.readonly_token = (readonly_token or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.admin_token or self.readonly_token)

    def role_for(self, token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        token = token.strip()
        if self.admin_token and hmac.compare_digest(token, self.admin_token):
            return ROLE_ADMIN
        if self.readonly_token and hmac.compare_digest(token, self.readonly_token):
            return ROLE_READONLY
        return None


def load_token_store(cfg: Any) -> tuple[TokenStore, dict]:
    """读取/生成 MCP token。

    优先配置 `mcp.auth_token` / 环境变量；都没有时生成随机 token 并写入
    `mcp.token_file`（0600），重启后复用，避免每次重启都要改客户端配置。
    """
    admin = cfg.mcp_auth_token
    readonly = cfg.mcp_readonly_token
    info: dict[str, Any] = {"generated": False, "path": None}

    if not admin and not readonly and not cfg.mcp_allow_unauthenticated:
        path: Path = cfg.mcp_token_file
        info["path"] = str(path)
        if path.exists():
            try:
                admin = path.read_text(encoding="utf-8").strip()
            except OSError:
                admin = ""
        if not admin:
            admin = secrets.token_urlsafe(32)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(admin + "\n", encoding="utf-8")
                os.chmod(path, 0o600)
                info["generated"] = True
            except OSError:
                # 写不进去时仍使用本次生成的 token（只在本进程有效）
                info["path"] = None
    elif admin and cfg.mcp_token_file.exists():
        info["path"] = str(cfg.mcp_token_file)

    return TokenStore(admin, readonly), info


class MCPAuthMiddleware:
    """在 MCP 应用最外层校验 Bearer token。

    认证顺序：
    1. 配置的静态 service token（mcp.auth_token / readonly_token）；
    2. 个人 API Key（ebk_...）：按 SHA-256 查 users.api_key_hash，
       角色由用户 privilege 决定（>=1 可写，0 只读）。
    """

    def __init__(self, app: ASGIApp, store: TokenStore,
                 allow_unauthenticated: bool = False,
                 user_token_lookup: Optional[Callable[[str], Optional[str]]] = None):
        self.app = app
        self.store = store
        self.allow_unauthenticated = allow_unauthenticated
        self.user_token_lookup = user_token_lookup or self._lookup_user_role

    def _lookup_user_role(self, token: str) -> Optional[str]:
        from .auth import lookup_user_by_api_key, touch_api_key_usage
        from .database import _get_session_factory

        db = _get_session_factory()()
        try:
            user = lookup_user_by_api_key(db, token)
            if user is None:
                return None
            touch_api_key_usage(db, user)
            return ROLE_ADMIN if int(user.privilege) >= 1 else ROLE_READONLY
        except Exception:
            # 数据库异常时认证失败关闭，避免退化成未认证放行
            logger.warning("个人 API Key 校验失败", exc_info=True)
            return None
        finally:
            db.close()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if self.allow_unauthenticated:
            token = _role.set(ROLE_ADMIN)
            try:
                await self.app(scope, receive, send)
            finally:
                _role.reset(token)
            return

        header = _header(scope, b"authorization")
        token_value = ""
        if header:
            text = header.strip()
            if text.lower().startswith("bearer "):
                token_value = text[7:].strip()

        role = self.store.role_for(token_value)
        if role is None and token_value.startswith(API_KEY_PREFIX):
            try:
                # SQLAlchemy 查询是同步的，放到线程池避免阻塞事件循环
                role = await asyncio.to_thread(self.user_token_lookup, token_value)
            except Exception:
                logger.warning("个人 API Key 查询异常", exc_info=True)
                role = None
        if role is None:
            await _send_unauthorized(send)
            return

        token = _role.set(role)
        try:
            await self.app(scope, receive, send)
        finally:
            _role.reset(token)


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


async def _send_unauthorized(send: Send) -> None:
    body = json.dumps(
        {"error": "unauthorized", "detail": "缺少或无效的 MCP Authorization Bearer token"},
        ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"www-authenticate", b'Bearer realm="ebaseline-mcp"'),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def current_role() -> Optional[str]:
    return _role.get()


def require_admin(func):
    """只允许 admin 角色调用；只读/未认证调用返回错误 JSON。"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        role = _role.get()
        if role == ROLE_ADMIN:
            return func(*args, **kwargs)
        if role == ROLE_READONLY:
            return json.dumps(
                {"error": "当前 MCP 凭证为只读，无权调用该工具"},
                ensure_ascii=False)
        return json.dumps(
            {"error": "未认证的 MCP 调用"}, ensure_ascii=False)

    return wrapper
