"""SQLAlchemy 模型：对应 init.sql 中 6 张表。"""
from __future__ import annotations

import ast
import json
import re
from datetime import datetime
from typing import Optional

from sqlalchemy import Integer, String, Text, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from .database import _get_engine, _get_session_factory
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    password: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    privilege: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 0=普通用户(测试权限), 1=管理员(管理任务和任务组), 2=超级管理员(全部权限)
    # 个人 MCP API Key：只存 SHA-256 哈希，明文只在生成时返回一次
    api_key_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True)
    api_key_prefix: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    api_key_created_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, nullable=True)
    api_key_last_used_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, nullable=True)

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key_hash)


class TaskGroup(Base):
    __tablename__ = "task_group"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    variables: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON string
    python_version: Mapped[str] = mapped_column(String(16), nullable=False, default="3.11")  # 工作空间 Python 版本


class TaskList(Base):
    __tablename__ = "task_list"

    tid: Mapped[int] = mapped_column(Integer, primary_key=True)
    gid: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    depends: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)  # 逗号分隔的 tid
    pre_script: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    input: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON string
    timeout: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Agent 调用超时（秒），NULL=全局默认
    expect: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    clean: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)


class Subject(Base):
    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False, default="")  # 名称/代号
    ipaddr: Mapped[str] = mapped_column(String(32), nullable=False, default="127.0.0.1")
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=8642)
    secret: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    api_mode: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 0=OpenAI Responses API 模式


class TaskBatch(Base):
    __tablename__ = "task_batch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sid: Mapped[int] = mapped_column(Integer, nullable=False)  # subject id
    gid: Mapped[int] = mapped_column(Integer, nullable=False)  # group id
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 0=未执行, 1=执行中, 2=已完成, 3=执行失败, 4=部分成功, 5=已取消
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 已执行的轮次
    manifest: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 创建时冻结的任务/变量/脚本 hash
    create_time: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, default=datetime.utcnow)


class TaskResult(Base):
    __tablename__ = "task_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)  # 所属批次
    sid: Mapped[int] = mapped_column(Integer, nullable=False)
    gid: Mapped[int] = mapped_column(Integer, nullable=False)
    tid: Mapped[int] = mapped_column(Integer, nullable=False)
    result: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 0=未执行, 1=执行成功, 2=执行失败, 3=执行中
    result_detail: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 3=执行成功但未配置校验, 2=完全成功(已校验通过), 1=校验通过但 Agent 异常, 0=失败
    attempt: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 所属批次执行轮次
    metrics: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON：耗时/token/轮次/工具调用等
    process: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, default=datetime.utcnow)


def _legacy_result_detail(process: Optional[str]) -> Optional[int]:
    """从旧日志的收尾行恢复细分状态（避免用 LIKE 误匹配 Agent 正文）。"""
    if not process:
        return None
    m = re.search(r"=== 任务 \d+ (成功|未校验|校验通过\(Agent异常\)|失败) \(耗时", process)
    if not m:
        return None
    return {"成功": 2, "未校验": 3, "校验通过(Agent异常)": 1, "失败": 0}[m.group(1)]


def _parse_legacy_metrics(process: Optional[str], result: int) -> Optional[dict]:
    """从旧版自由文本日志里尽量恢复耗时/轮次/token，供新指标页展示。"""
    if not process:
        return None
    metrics: dict = {"metrics_version": 1}

    m = re.search(r"\(耗时 ([\d.]+)s\) ===", process)
    if m:
        metrics["duration_sec"] = float(m.group(1))

    m = re.search(r"耗时: ([\d.]+)s, 轮次: (\d+)", process)
    if m:
        metrics["agent_duration_sec"] = float(m.group(1))
        metrics["rounds"] = int(m.group(2))

    m = re.search(r"tokens: (\{[^\n]*\})", process)
    if m:
        try:
            tokens = ast.literal_eval(m.group(1))
        except (ValueError, SyntaxError):
            tokens = None
        if isinstance(tokens, dict):
            metrics["tokens"] = {
                "input": tokens.get("input_tokens"),
                "output": tokens.get("output_tokens"),
                "total": tokens.get("total_tokens"),
            }

    if "依赖检查: 失败" in process:
        metrics["stage"] = "dependency"
    elif "阶段 2: 前置脚本" in process and "阶段 4: 调用 Agent" not in process:
        metrics["stage"] = "pre_script"
    elif result == 1:
        metrics["stage"] = "ok"
    elif result == 2:
        metrics["stage"] = "agent" if "Agent 调用: 失败" in process else "expect"

    return metrics or None


def _ensure_columns() -> None:
    """为已存在的库补齐新增列（create_all 不会修改已有表）。"""
    from sqlalchemy import inspect, text

    engine = _get_engine()
    insp = inspect(engine)
    tables = set(insp.get_table_names())

    if "users" in tables:
        existing = {c["name"] for c in insp.get_columns("users")}
        additions = [
            ("api_key_hash", "ALTER TABLE users ADD COLUMN api_key_hash CHAR(64) NULL"),
            ("api_key_prefix", "ALTER TABLE users ADD COLUMN api_key_prefix VARCHAR(16) NULL"),
            ("api_key_created_at",
             "ALTER TABLE users ADD COLUMN api_key_created_at TIMESTAMP NULL"),
            ("api_key_last_used_at",
             "ALTER TABLE users ADD COLUMN api_key_last_used_at TIMESTAMP NULL"),
        ]
        pending = [ddl for name, ddl in additions if name not in existing]
        if pending:
            with engine.begin() as conn:
                for ddl in pending:
                    conn.execute(text(ddl))
        user_indexes = {i["name"] for i in insp.get_indexes("users")}
        if "ix_users_api_key_hash" not in user_indexes:
            with engine.begin() as conn:
                conn.execute(text(
                    "CREATE INDEX ix_users_api_key_hash ON users (api_key_hash)"
                ))

    if "task_batch" in tables:
        existing = {c["name"] for c in insp.get_columns("task_batch")}
        additions = [
            ("attempt", "ALTER TABLE task_batch ADD COLUMN attempt INT NOT NULL DEFAULT 0"),
            ("manifest", "ALTER TABLE task_batch ADD COLUMN manifest TEXT NULL"),
        ]
        pending = [ddl for name, ddl in additions if name not in existing]
        if pending:
            with engine.begin() as conn:
                for ddl in pending:
                    conn.execute(text(ddl))

    if "task_result" not in tables:
        return

    existing = {c["name"] for c in insp.get_columns("task_result")}
    additions = [
        ("batch_id", "ALTER TABLE task_result ADD COLUMN batch_id INT NULL"),
        ("result_detail", "ALTER TABLE task_result ADD COLUMN result_detail INT NULL"),
        ("attempt", "ALTER TABLE task_result ADD COLUMN attempt INT NULL"),
        ("metrics", "ALTER TABLE task_result ADD COLUMN metrics TEXT NULL"),
    ]
    pending = [ddl for name, ddl in additions if name not in existing]
    if pending:
        with engine.begin() as conn:
            for ddl in pending:
                conn.execute(text(ddl))

    indexes = {i["name"] for i in insp.get_indexes("task_result")}
    if "ix_task_result_batch_id" not in indexes:
        with engine.begin() as conn:
            conn.execute(text("CREATE INDEX ix_task_result_batch_id ON task_result (batch_id)"))
    if "ix_task_result_batch_attempt" not in indexes:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE INDEX ix_task_result_batch_attempt ON task_result (batch_id, attempt)"
            ))

    # 旧数据没有批次归属：旧版执行前会按 sid+gid 删除历史结果，
    # 因此现存结果一定属于该 sid+gid 最近一次批次。
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE task_result r "
            "JOIN (SELECT sid, gid, MAX(id) AS bid FROM task_batch GROUP BY sid, gid) b "
            "  ON r.sid = b.sid AND r.gid = b.gid "
            "SET r.batch_id = b.bid "
            "WHERE r.batch_id IS NULL"
        ))
    # 旧结果没有结构化指标：从日志里恢复耗时/轮次/token 与细分状态。
    # 新版 metrics 一定带 metrics_version，用它区分新老数据。
    session = _get_session_factory()()
    try:
        rows = session.query(TaskResult).filter(
            (TaskResult.metrics.is_(None))
            | (~TaskResult.metrics.like('%metrics_version%'))
        ).all()
        for row in rows:
            detail = _legacy_result_detail(row.process)
            if detail is not None:
                row.result_detail = detail
            if row.metrics is None:
                parsed = _parse_legacy_metrics(row.process, row.result)
                if parsed:
                    row.metrics = json.dumps(parsed, ensure_ascii=False)
            else:
                # 已回填过但没有版本标记的旧 metrics，补上版本避免每次启动重扫
                try:
                    data = json.loads(row.metrics)
                except (TypeError, ValueError):
                    data = None
                if isinstance(data, dict) and "metrics_version" not in data:
                    data["metrics_version"] = 1
                    row.metrics = json.dumps(data, ensure_ascii=False)
        session.commit()
    finally:
        session.close()


def create_all():
    """创建所有表（如果不存在），并补齐已存在表的列。"""
    Base.metadata.create_all(bind=_get_engine())
    _ensure_columns()
