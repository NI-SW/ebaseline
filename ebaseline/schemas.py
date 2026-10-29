"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Auth ───────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=32)
    password: str = Field(..., min_length=6, max_length=128)
    email: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class UserResponse(BaseModel):
    id: int
    username: str
    email: str | None
    privilege: int

    class Config:
        from_attributes = True


class UserPrivilegeUpdate(BaseModel):
    privilege: int = Field(..., ge=0, le=2)


# ── Task Group ─────────────────────────────────────────────────
class TaskGroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=32)
    description: str | None = Field(None, max_length=128)
    variables: dict[str, Any] | None = None
    python_version: str = Field("3.11", max_length=16)


class TaskGroupUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=32)
    description: str | None = Field(None, max_length=128)
    variables: dict[str, Any] | None = None
    python_version: str | None = Field(None, max_length=16)


class TaskGroupResponse(BaseModel):
    id: int
    name: str | None
    description: str | None
    variables: dict[str, Any] | None = None
    python_version: str = "3.11"

    class Config:
        from_attributes = True


# ── Task ───────────────────────────────────────────────────────
class TaskCreate(BaseModel):
    tid: int | None = None  # 不填则自动分配
    name: str = Field(..., min_length=1, max_length=32)
    description: str | None = Field(None, max_length=128)
    depends: list[int] | None = None  # 依赖的 tid 列表
    pre_script: str | None = Field(None, max_length=128)
    input: dict[str, Any] | None = None  # {prompt, conversation?, timeout?}
    expect: str | None = Field(None, max_length=128)
    clean: str | None = Field(None, max_length=128)


class TaskUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=32)
    description: str | None = Field(None, max_length=128)
    depends: list[int] | None = None
    pre_script: str | None = Field(None, max_length=128)
    input: dict[str, Any] | None = None
    expect: str | None = Field(None, max_length=128)
    clean: str | None = Field(None, max_length=128)


class TaskResponse(BaseModel):
    tid: int
    gid: int
    name: str | None
    description: str | None
    depends: list[int] | None
    pre_script: str | None
    input: dict[str, Any] | None
    expect: str | None
    clean: str | None

    class Config:
        from_attributes = True


# ── Subject ────────────────────────────────────────────────────
class SubjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=32)
    ipaddr: str = Field(..., max_length=32)
    port: int = Field(..., gt=0, le=65535)
    secret: str = Field("", max_length=128)
    api_mode: int = Field(0, ge=0)


class SubjectUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=32)
    ipaddr: str | None = Field(None, max_length=32)
    port: int | None = Field(None, gt=0, le=65535)
    secret: str | None = Field(None, max_length=128)
    api_mode: int | None = Field(None, ge=0)


class SubjectResponse(BaseModel):
    id: int
    name: str
    ipaddr: str
    port: int
    api_mode: int

    class Config:
        from_attributes = True


# ── Batch ──────────────────────────────────────────────────────
class BatchCreate(BaseModel):
    sid: int
    gid: int
    tids: list[int] | None = None  # 不填则执行组内全部任务


class BatchResponse(BaseModel):
    id: int
    sid: int
    gid: int
    status: int
    create_time: datetime

    class Config:
        from_attributes = True


class BatchDetailResponse(BatchResponse):
    tasks: list[dict[str, Any]] | None = None  # 包含每个任务的执行状态


# ── Result ─────────────────────────────────────────────────────
class ResultResponse(BaseModel):
    id: int
    sid: int
    gid: int
    tid: int
    result: int
    process: str | None
    create_time: datetime

    class Config:
        from_attributes = True


class ResultSubmitRequest(BaseModel):
    """智能体主动上报结果（MCP 用）。"""
    result: int = Field(..., ge=0, le=2)
    process: str | None = None
