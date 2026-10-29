"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional,  Any

from pydantic import BaseModel, Field


# ── Auth ───────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=32)
    password: str = Field(..., min_length=6, max_length=128)
    email: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class UserResponse(BaseModel):
    id: int
    username: str
    email: Optional[str]
    privilege: int

    class Config:
        from_attributes = True


class UserPrivilegeUpdate(BaseModel):
    privilege: int = Field(..., ge=0, le=2)


# ── Task Group ─────────────────────────────────────────────────
class TaskGroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=32)
    description: Optional[str] = Field(None, max_length=128)
    variables: Optional[dict[str, Any]] = None
    python_version: str = Field("3.11", max_length=16)


class TaskGroupUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=32)
    description: Optional[str] = Field(None, max_length=128)
    variables: Optional[dict[str, Any]] = None
    python_version: Optional[str] = Field(None, max_length=16)


class TaskGroupResponse(BaseModel):
    id: int
    name: Optional[str]
    description: Optional[str]
    variables: Optional[dict[str, Any]] = None
    python_version: str = "3.11"

    class Config:
        from_attributes = True


# ── Task ───────────────────────────────────────────────────────
class TaskCreate(BaseModel):
    tid: Optional[int] = None  # 不填则自动分配
    name: str = Field(..., min_length=1, max_length=32)
    description: Optional[str] = Field(None, max_length=128)
    depends: Optional[list[int]] = None  # 依赖的 tid 列表
    pre_script: Optional[str] = Field(None, max_length=128)
    input: Optional[dict[str, Any]] = None  # {prompt, conversation?, timeout?}
    expect: Optional[str] = Field(None, max_length=128)
    clean: Optional[str] = Field(None, max_length=128)


class TaskUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=32)
    description: Optional[str] = Field(None, max_length=128)
    depends: Optional[list[int]] = None
    pre_script: Optional[str] = Field(None, max_length=128)
    input: Optional[dict[str, Any]] = None
    expect: Optional[str] = Field(None, max_length=128)
    clean: Optional[str] = Field(None, max_length=128)


class TaskResponse(BaseModel):
    tid: int
    gid: int
    name: Optional[str]
    description: Optional[str]
    depends: Optional[list[int]]
    pre_script: Optional[str]
    input: Optional[dict[str, Any]]
    expect: Optional[str]
    clean: Optional[str]

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
    name: Optional[str] = Field(None, min_length=1, max_length=32)
    ipaddr: Optional[str] = Field(None, max_length=32)
    port: Optional[int] = Field(None, gt=0, le=65535)
    secret: Optional[str] = Field(None, max_length=128)
    api_mode: Optional[int] = Field(None, ge=0)


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
    tids: Optional[list[int]] = None  # 不填则执行组内全部任务


class BatchResponse(BaseModel):
    id: int
    sid: int
    gid: int
    status: int
    create_time: datetime

    class Config:
        from_attributes = True


class BatchDetailResponse(BatchResponse):
    tasks: Optional[list[dict[str, Any]]] = None  # 包含每个任务的执行状态


# ── Result ─────────────────────────────────────────────────────
class ResultResponse(BaseModel):
    id: int
    sid: int
    gid: int
    tid: int
    result: int
    process: Optional[str]
    create_time: datetime

    class Config:
        from_attributes = True


class ResultSubmitRequest(BaseModel):
    """智能体主动上报结果（MCP 用）。"""
    result: int = Field(..., ge=0, le=2)
    process: Optional[str] = None
