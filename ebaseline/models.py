"""SQLAlchemy 模型：对应 init.sql 中 6 张表。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Integer, String, Text, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from .database import _get_engine
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
    name: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    depends: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)  # 逗号分隔的 tid
    pre_script: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    input: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON string
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
    # 0=未执行, 1=执行中, 2=已完成, 3=执行失败, 4=部分成功
    create_time: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, default=datetime.utcnow)


class TaskResult(Base):
    __tablename__ = "task_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sid: Mapped[int] = mapped_column(Integer, nullable=False)
    gid: Mapped[int] = mapped_column(Integer, nullable=False)
    tid: Mapped[int] = mapped_column(Integer, nullable=False)
    result: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 0=未执行, 1=执行成功, 2=执行失败
    process: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, default=datetime.utcnow)


def create_all():
    """创建所有表（如果不存在）。生产环境建议先执行 init.sql。"""
    Base.metadata.create_all(bind=_get_engine())
