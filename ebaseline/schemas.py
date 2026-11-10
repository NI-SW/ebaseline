"""Pydantic 请求/响应模型。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional,  Any

from pydantic import BaseModel, Field, field_validator


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
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = Field(None, max_length=128)
    depends: Optional[list[int]] = None  # 依赖的 tid 列表
    pre_script: Optional[str] = Field(None, max_length=128)
    input: Optional[dict[str, Any]] = None  # {prompt, conversation?}
    timeout: Optional[int] = Field(None, ge=1, le=86400)  # Agent 调用超时（秒），None=全局默认
    expect: Optional[str] = Field(None, max_length=128)
    clean: Optional[str] = Field(None, max_length=128)


class TaskUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    description: Optional[str] = Field(None, max_length=128)
    depends: Optional[list[int]] = None
    pre_script: Optional[str] = Field(None, max_length=128)
    input: Optional[dict[str, Any]] = None
    timeout: Optional[int] = Field(None, ge=1, le=86400)  # 显式传 null 可恢复全局默认
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
    timeout: Optional[int] = None
    expect: Optional[str]
    clean: Optional[str]

    class Config:
        from_attributes = True


# ── Subject ────────────────────────────────────────────────────
def _normalize_api_path(value: Optional[str]) -> str:
    """规范化 Agent 端点路径：去掉空白与首尾斜杠，空值回退 v1。"""
    return (value or "").strip().strip("/") or "v1"


class SubjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=32)
    ipaddr: str = Field(..., max_length=32)
    port: int = Field(..., gt=0, le=65535)
    api_path: str = Field("v1", max_length=125)
    secret: str = Field("", max_length=128)
    api_mode: int = Field(0, ge=0)

    @field_validator("api_path")
    @classmethod
    def _norm_api_path(cls, value: str) -> str:
        return _normalize_api_path(value)


class SubjectUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=32)
    ipaddr: Optional[str] = Field(None, max_length=32)
    port: Optional[int] = Field(None, gt=0, le=65535)
    api_path: Optional[str] = Field(None, max_length=125)
    secret: Optional[str] = Field(None, max_length=128)
    api_mode: Optional[int] = Field(None, ge=0)

    @field_validator("api_path")
    @classmethod
    def _norm_api_path(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _normalize_api_path(value)


class SubjectResponse(BaseModel):
    id: int
    name: str
    ipaddr: str
    port: int
    api_path: str
    api_mode: int

    class Config:
        from_attributes = True


class SubjectTestRequest(BaseModel):
    """测试待测 agent 端点连通性（不落库）。"""
    ipaddr: str = Field(..., min_length=1, max_length=32)
    port: int = Field(..., gt=0, le=65535)
    api_path: str = Field("v1", max_length=125)
    secret: str = Field("", max_length=128)
    sid: Optional[int] = None  # 编辑已有主体时用于复用已保存的 API Key
    timeout: int = Field(60, ge=5, le=600)

    @field_validator("api_path")
    @classmethod
    def _norm_api_path(cls, value: str) -> str:
        return _normalize_api_path(value)


class SubjectTestResponse(BaseModel):
    ok: bool
    url: str
    latency_ms: int
    first_token_ms: Optional[int] = None
    output: str = ""
    error: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)


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
    metrics: Optional[dict[str, Any]] = None

    class Config:
        from_attributes = True


class BatchDetailResponse(BatchResponse):
    subject_name: Optional[str] = None
    group_name: Optional[str] = None
    tasks: Optional[list[dict[str, Any]]] = None  # 包含每个任务的执行状态与指标
    running_task_tid: Optional[int] = None  # 正在单独执行的任务 tid（服务端实时状态）


# ── Result ─────────────────────────────────────────────────────
class ResultResponse(BaseModel):
    id: int
    batch_id: Optional[int] = None
    sid: int
    gid: int
    tid: int
    result: int
    result_detail: Optional[int] = None
    metrics: Optional[dict[str, Any]] = None
    process: Optional[str]
    create_time: datetime

    @field_validator("metrics", mode="before")
    @classmethod
    def _parse_metrics(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return None
        return value

    class Config:
        from_attributes = True


class ResultSubmitRequest(BaseModel):
    """智能体主动上报结果（MCP 用）。"""
    result: int = Field(..., ge=0, le=2)
    process: Optional[str] = None
