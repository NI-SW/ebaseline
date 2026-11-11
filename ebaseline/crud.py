"""通用 CRUD 操作。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import manifest as manifest_mod
from . import models, schemas, task_graph
from .config import get_config
from .security import merge_masked_values

logger = logging.getLogger(__name__)


# ── Task Group ─────────────────────────────────────────────────
def create_task_group(db: Session, data: schemas.TaskGroupCreate) -> models.TaskGroup:
    variables = json.dumps(data.variables, ensure_ascii=False) if data.variables else None
    group = models.TaskGroup(
        name=data.name,
        description=data.description,
        variables=variables,
        python_version=data.python_version,
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    return group


def get_task_group(db: Session, gid: int) -> models.TaskGroup | None:
    return db.query(models.TaskGroup).filter(models.TaskGroup.id == gid).first()


def list_task_groups(db: Session, skip: int = 0, limit: int = 100) -> list[models.TaskGroup]:
    return db.query(models.TaskGroup).offset(skip).limit(limit).all()


def _merge_group_variables(old_raw: str | None, new_vars: dict | None) -> str | None:
    """更新组变量时用旧值回填脱敏占位符。"""
    if new_vars is None:
        return None
    old_vars: dict = {}
    if old_raw:
        try:
            parsed = json.loads(old_raw)
            if isinstance(parsed, dict):
                old_vars = parsed
        except (TypeError, ValueError):
            old_vars = {}
    cfg = get_config()
    merged = merge_masked_values(
        old_vars, new_vars, cfg.security_mask_placeholder)
    return json.dumps(merged, ensure_ascii=False)


def update_task_group(db: Session, gid: int, data: schemas.TaskGroupUpdate) -> models.TaskGroup | None:
    group = get_task_group(db, gid)
    if group is None:
        return None
    fields = data.model_fields_set
    if data.name is not None:
        group.name = data.name
    # description/variables 显式传 null 时清空，而不是忽略
    if "description" in fields:
        group.description = data.description
    if "variables" in fields:
        group.variables = _merge_group_variables(group.variables, data.variables)
    if data.python_version is not None:
        group.python_version = data.python_version
    db.commit()
    db.refresh(group)
    return group


def delete_task_group(db: Session, gid: int) -> bool:
    group = get_task_group(db, gid)
    if group is None:
        return False
    # 级联删除组内任务、批次与结果（表间无外键，需手动清理）
    db.query(models.TaskList).filter(models.TaskList.gid == gid).delete()
    db.query(models.TaskBatch).filter(models.TaskBatch.gid == gid).delete()
    db.query(models.TaskResult).filter(models.TaskResult.gid == gid).delete()
    db.delete(group)
    db.commit()
    return True


def _next_tid(db: Session, gid: int) -> int:
    """为组内任务分配 tid：gid * 10000 + 组内最大序号 + 1"""
    max_task = (
        db.query(models.TaskList)
        .filter(models.TaskList.gid == gid)
        .order_by(models.TaskList.tid.desc())
        .first()
    )
    if max_task is None:
        return gid * 10000 + 1
    return max_task.tid + 1


# ── Task ───────────────────────────────────────────────────────
def create_task(db: Session, gid: int, data: schemas.TaskCreate) -> models.TaskList:
    tid = data.tid if data.tid is not None else _next_tid(db, gid)
    depends = task_graph.depends_to_str(data.depends)
    input_str = json.dumps(data.input, ensure_ascii=False) if data.input else None
    task = models.TaskList(
        tid=tid,
        gid=gid,
        name=data.name,
        description=data.description,
        depends=depends,
        pre_script=data.pre_script,
        input=input_str,
        timeout=data.timeout,
        expect=data.expect,
        clean=data.clean,
    )
    # 保存前校验依赖：缺失/跨组/自依赖/环路直接拒绝，不再静默执行
    existing = db.query(models.TaskList).filter(models.TaskList.gid == gid).all()
    if any(t.tid == tid for t in existing):
        raise task_graph.DependencyError([f"任务 tid {tid} 已存在"])
    task_graph.ensure_valid(existing + [task])
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def get_task(db: Session, tid: int) -> models.TaskList | None:
    return db.query(models.TaskList).filter(models.TaskList.tid == tid).first()


def list_tasks(db: Session, gid: int | None = None) -> list[models.TaskList]:
    q = db.query(models.TaskList)
    if gid is not None:
        q = q.filter(models.TaskList.gid == gid)
    return q.all()


def update_task(db: Session, tid: int, data: schemas.TaskUpdate) -> models.TaskList | None:
    task = get_task(db, tid)
    if task is None:
        return None
    fields = data.model_fields_set
    if data.name is not None:
        task.name = data.name
    # 显式 null 表示清空；未提供则保持原值
    if "description" in fields:
        task.description = data.description
    if "depends" in fields:
        task.depends = task_graph.depends_to_str(data.depends)
        existing = (
            db.query(models.TaskList)
            .filter(models.TaskList.gid == task.gid, models.TaskList.tid != task.tid)
            .all()
        )
        task_graph.ensure_valid(existing + [task])
    if "pre_script" in fields:
        task.pre_script = data.pre_script
    if "input" in fields:
        task.input = json.dumps(data.input, ensure_ascii=False) if data.input else None
    # 显式传 null 时清除任务级超时，恢复全局默认
    if "timeout" in fields:
        task.timeout = data.timeout
    if "expect" in fields:
        task.expect = data.expect
    if "clean" in fields:
        task.clean = data.clean
    db.commit()
    db.refresh(task)
    return task


def delete_task(db: Session, tid: int) -> bool:
    task = get_task(db, tid)
    if task is None:
        return False
    db.delete(task)
    db.commit()
    return True


# ── Subject ────────────────────────────────────────────────────
def create_subject(db: Session, data: schemas.SubjectCreate) -> models.Subject:
    subject = models.Subject(
        name=data.name,
        ipaddr=data.ipaddr,
        port=data.port,
        secret=data.secret,
        api_mode=data.api_mode,
    )
    db.add(subject)
    db.commit()
    db.refresh(subject)
    return subject


def get_subject(db: Session, sid: int) -> models.Subject | None:
    return db.query(models.Subject).filter(models.Subject.id == sid).first()


def list_subjects(db: Session) -> list[models.Subject]:
    return db.query(models.Subject).all()


def update_subject(db: Session, sid: int, data: schemas.SubjectUpdate) -> models.Subject | None:
    subject = get_subject(db, sid)
    if subject is None:
        return None
    if data.name is not None:
        subject.name = data.name
    if data.ipaddr is not None:
        subject.ipaddr = data.ipaddr
    if data.port is not None:
        subject.port = data.port
    if data.secret is not None:
        subject.secret = data.secret
    if data.api_mode is not None:
        subject.api_mode = data.api_mode
    db.commit()
    db.refresh(subject)
    return subject


def delete_subject(db: Session, sid: int) -> bool:
    subject = get_subject(db, sid)
    if subject is None:
        return False
    db.delete(subject)
    db.commit()
    return True


# ── Batch ──────────────────────────────────────────────────────
def create_batch(db: Session, sid: int, gid: int,
                 manifest: dict | None = None) -> models.TaskBatch:
    """创建批次并冻结任务/变量/脚本 hash 清单。"""
    manifest_data = manifest
    if manifest_data is None:
        try:
            manifest_data = manifest_mod.build_manifest(db, sid, gid)
        except Exception as exc:  # 清单失败不应阻塞创建，但必须可追溯
            logger.warning("构建批次清单失败: %s", exc, exc_info=True)
            manifest_data = {
                "manifest_version": 0,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "error": f"{type(exc).__name__}: {exc}",
                "tasks": [],
            }
    batch = models.TaskBatch(
        sid=sid,
        gid=gid,
        status=0,
        attempt=0,
        manifest=json.dumps(manifest_data, ensure_ascii=False),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def get_batch(db: Session, batch_id: int) -> models.TaskBatch | None:
    return db.query(models.TaskBatch).filter(models.TaskBatch.id == batch_id).first()


def list_batches(db: Session, skip: int = 0, limit: int = 100) -> list[models.TaskBatch]:
    return db.query(models.TaskBatch).order_by(models.TaskBatch.id.desc()).offset(skip).limit(limit).all()


def update_batch_status(db: Session, batch_id: int, status: int) -> models.TaskBatch | None:
    batch = get_batch(db, batch_id)
    if batch is None:
        return None
    batch.status = status
    db.commit()
    db.refresh(batch)
    return batch


def increment_batch_attempt(db: Session, batch_id: int) -> int:
    """整批重跑开始前递增 attempt，返回本次轮次。"""
    batch = get_batch(db, batch_id)
    if batch is None:
        return 0
    batch.attempt = int(batch.attempt or 0) + 1
    db.commit()
    db.refresh(batch)
    return int(batch.attempt)


def delete_batch(db: Session, batch_id: int) -> bool:
    batch = get_batch(db, batch_id)
    if batch is None:
        return False
    # 只删除该批次自己的结果，避免误删同主体/同组的其他批次
    db.query(models.TaskResult).filter(
        models.TaskResult.batch_id == batch.id,
    ).delete()
    db.delete(batch)
    db.commit()
    return True


# ── Result ─────────────────────────────────────────────────────
def create_result(
    db: Session,
    sid: int,
    gid: int,
    tid: int,
    result: int,
    process: str | None,
    batch_id: int | None = None,
    result_detail: int | None = None,
    metrics: str | None = None,
    attempt: int | None = None,
) -> models.TaskResult:
    r = models.TaskResult(
        batch_id=batch_id,
        sid=sid,
        gid=gid,
        tid=tid,
        result=result,
        result_detail=result_detail,
        attempt=attempt,
        metrics=metrics,
        process=process,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def update_result(
    db: Session,
    result_id: int,
    result: int | None = None,
    process: str | None = None,
    result_detail: int | None = None,
    metrics: str | None = None,
) -> models.TaskResult | None:
    """更新已有结果（用于执行中实时追加日志）。"""
    r = get_result(db, result_id)
    if r is None:
        return None
    if result is not None:
        r.result = result
    if result_detail is not None:
        r.result_detail = result_detail
    if metrics is not None:
        r.metrics = metrics
    if process is not None:
        r.process = process
    db.commit()
    db.refresh(r)
    return r


def get_result(db: Session, result_id: int) -> models.TaskResult | None:
    return db.query(models.TaskResult).filter(models.TaskResult.id == result_id).first()


def list_results(
    db: Session,
    sid: int | None = None,
    gid: int | None = None,
    tid: int | None = None,
    result: int | None = None,
    batch_id: int | None = None,
    attempt: int | None = None,
) -> list[models.TaskResult]:
    q = db.query(models.TaskResult)
    if batch_id is not None:
        q = q.filter(models.TaskResult.batch_id == batch_id)
    if attempt is not None:
        if attempt == 0:
            # attempt 0 兼容没有该字段的旧结果（NULL）
            q = q.filter(or_(
                models.TaskResult.attempt == 0,
                models.TaskResult.attempt.is_(None),
            ))
        else:
            q = q.filter(models.TaskResult.attempt == attempt)
    if sid is not None:
        q = q.filter(models.TaskResult.sid == sid)
    if gid is not None:
        q = q.filter(models.TaskResult.gid == gid)
    if tid is not None:
        q = q.filter(models.TaskResult.tid == tid)
    if result is not None:
        q = q.filter(models.TaskResult.result == result)
    return q.order_by(models.TaskResult.id.desc()).all()


def list_results_for_batches(db: Session, batch_ids: list[int]) -> list[models.TaskResult]:
    """一次取出多个批次的结果（批次列表聚合用）。"""
    if not batch_ids:
        return []
    return (
        db.query(models.TaskResult)
        .filter(models.TaskResult.batch_id.in_(batch_ids))
        .order_by(models.TaskResult.id.asc())
        .all()
    )


def count_tasks_by_gid(db: Session, gids: set[int] | list[int]) -> dict[int, int]:
    """按任务组统计任务数（批次指标中的“任务数”口径）。"""
    if not gids:
        return {}
    rows = (
        db.query(models.TaskList.gid, func.count(models.TaskList.tid))
        .filter(models.TaskList.gid.in_(list(gids)))
        .group_by(models.TaskList.gid)
        .all()
    )
    return {gid: int(count) for gid, count in rows}


def delete_results_by_batch(db: Session, batch_id: int) -> int:
    """删除指定批次的结果，返回删除条数。"""
    count = db.query(models.TaskResult).filter(
        models.TaskResult.batch_id == batch_id,
    ).delete()
    db.commit()
    return count


def get_task_result(
    db: Session,
    sid: int,
    gid: int,
    tid: int,
    batch_id: int | None = None,
) -> models.TaskResult | None:
    """获取某任务的最新结果；指定 batch_id 时只在该批次内查找。"""
    q = db.query(models.TaskResult).filter(
        models.TaskResult.sid == sid,
        models.TaskResult.gid == gid,
        models.TaskResult.tid == tid,
    )
    if batch_id is not None:
        q = q.filter(models.TaskResult.batch_id == batch_id)
    return q.order_by(models.TaskResult.id.desc()).first()


def get_batch_task_result(db: Session, batch_id: int, tid: int,
                          attempt: int | None = None) -> models.TaskResult | None:
    """获取批次内某任务的最新结果；指定 attempt 时只看该轮。"""
    q = db.query(models.TaskResult).filter(
        models.TaskResult.batch_id == batch_id,
        models.TaskResult.tid == tid,
    )
    if attempt is not None:
        if attempt == 0:
            q = q.filter(or_(
                models.TaskResult.attempt == 0,
                models.TaskResult.attempt.is_(None),
            ))
        else:
            q = q.filter(models.TaskResult.attempt == attempt)
    return q.order_by(models.TaskResult.id.desc()).first()


def list_batch_attempts(db: Session, batch_id: int) -> list[int]:
    """批次实际产生过结果的 attempt 列表（升序）。"""
    rows = (
        db.query(models.TaskResult.attempt)
        .filter(
            models.TaskResult.batch_id == batch_id,
            models.TaskResult.attempt.isnot(None),
        )
        .distinct()
        .order_by(models.TaskResult.attempt.asc())
        .all()
    )
    return [int(r[0]) for r in rows if r[0] is not None]
