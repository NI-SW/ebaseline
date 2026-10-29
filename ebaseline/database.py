"""数据库连接管理：SQLAlchemy 引擎与会话工厂。"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_config

_engine = None
_SessionLocal: sessionmaker | None = None


def _get_engine():
    global _engine
    if _engine is None:
        cfg = get_config()
        _engine = create_engine(
            cfg.db_url,
            pool_pre_ping=True,
            pool_recycle=3600,
            echo=cfg.server_debug,
        )
    return _engine


def _get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_get_engine())
    return _SessionLocal


def get_db() -> Session:
    """FastAPI 依赖注入用：获取数据库会话。"""
    db = _get_session_factory()()
    try:
        yield db
    finally:
        db.close()
