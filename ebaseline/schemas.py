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
    # 个人 MCP API Key 状态：只暴露前缀/时间，绝不返回哈希或明文
    has_api_key: bool = False
    api_key_prefix: Optional[str] = None
    api_key_created_at: Optional[datetime] = None
    api_key_last_used_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class UserPrivilegeUpdate(BaseModel):
    privilege: int = Field(..., ge=0, le=2)


class ProfileUpdate(BaseModel):
    """当前用户修改自己的用户名/邮箱，必须验证当前密码。"""
    username: Optional[str] = Field(None, min_length=1, max_length=32)
    email: Optional[str] = Field(None, max_length=128)
    current_password: str = Field(..., min_length=1, max_length=128)


class PasswordChange(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=6, max_length=128)


class ApiKeyResponse(BaseModel):
    api_key: str          # 明文，仅本次响应返回
    prefix: str
    created_at: datetime
    message: str = "API Key 已生成，请立即保存；关闭弹窗后无法再次查看"


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
    attempt: int = 0  # 已执行的轮次（每次整批重跑 +1）
    create_time: datetime
    metrics: Optional[dict[str, Any]] = None

    class Config:
        from_attributes = True


class BatchDetailResponse(BatchResponse):
    subject_name: Optional[str] = None
    group_name: Optional[str] = None
    tasks: Optional[list[dict[str, Any]]] = None  # 包含每个任务的执行状态与指标
    running_task_tid: Optional[int] = None  # 正在单独执行的任务 tid（服务端实时状态）
    manifest_summary: Optional[dict[str, Any]] = None  # 冻结清单摘要（变量已脱敏）
    available_attempts: Optional[list[int]] = None
    viewing_attempt: Optional[int] = None


# ── Result ─────────────────────────────────────────────────────
class ResultResponse(BaseModel):
    id: int
    batch_id: Optional[int] = None
    sid: int
    gid: int
    tid: int
    result: int
    result_detail: Optional[int] = None
    attempt: Optional[int] = None
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
