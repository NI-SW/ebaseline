"""通用 CRUD 操作。"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from . import models, schemas


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


def update_task_group(db: Session, gid: int, data: schemas.TaskGroupUpdate) -> models.TaskGroup | None:
    group = get_task_group(db, gid)
    if group is None:
        return None
    if data.name is not None:
        group.name = data.name
    if data.description is not None:
        group.description = data.description
    if data.variables is not None:
        group.variables = json.dumps(data.variables, ensure_ascii=False)
    if data.python_version is not None:
        group.python_version = data.python_version
    db.commit()
    db.refresh(group)
    return group


def delete_task_group(db: Session, gid: int) -> bool:
    group = get_task_group(db, gid)
    if group is None:
        return False
    # 级联删除组内任务
    db.query(models.TaskList).filter(models.TaskList.gid == gid).delete()
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
    depends = ",".join(map(str, data.depends)) if data.depends else None
    input_str = json.dumps(data.input, ensure_ascii=False) if data.input else None
    task = models.TaskList(
        tid=tid,
        gid=gid,
        name=data.name,
        description=data.description,
        depends=depends,
        pre_script=data.pre_script,
        input=input_str,
        expect=data.expect,
        clean=data.clean,
    )
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
    if data.name is not None:
        task.name = data.name
    if data.description is not None:
        task.description = data.description
    if data.depends is not None:
        task.depends = ",".join(map(str, data.depends)) if data.depends else None
    if data.pre_script is not None:
        task.pre_script = data.pre_script
    if data.input is not None:
        task.input = json.dumps(data.input, ensure_ascii=False)
    if data.expect is not None:
        task.expect = data.expect
    if data.clean is not None:
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
def create_batch(db: Session, sid: int, gid: int) -> models.TaskBatch:
    batch = models.TaskBatch(sid=sid, gid=gid, status=0)
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


def delete_batch(db: Session, batch_id: int) -> bool:
    batch = get_batch(db, batch_id)
    if batch is None:
        return False
    # 级联删除该批次的结果
    db.query(models.TaskResult).filter(
        models.TaskResult.sid == batch.sid,
        models.TaskResult.gid == batch.gid,
    ).delete()
    db.delete(batch)
    db.commit()
    return True


# ── Result ─────────────────────────────────────────────────────
def create_result(db: Session, sid: int, gid: int, tid: int, result: int, process: str | None) -> models.TaskResult:
    r = models.TaskResult(sid=sid, gid=gid, tid=tid, result=result, process=process)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def update_result(db: Session, result_id: int, result: int | None = None, process: str | None = None) -> models.TaskResult | None:
    """更新已有结果（用于执行中实时追加日志）。"""
    r = get_result(db, result_id)
    if r is None:
        return None
    if result is not None:
        r.result = result
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
) -> list[models.TaskResult]:
    q = db.query(models.TaskResult)
    if sid is not None:
        q = q.filter(models.TaskResult.sid == sid)
    if gid is not None:
        q = q.filter(models.TaskResult.gid == gid)
    if tid is not None:
        q = q.filter(models.TaskResult.tid == tid)
    if result is not None:
        q = q.filter(models.TaskResult.result == result)
    return q.order_by(models.TaskResult.id.desc()).all()


def delete_results_by_batch(db: Session, sid: int, gid: int) -> int:
    """删除指定批次对应的所有结果，返回删除条数。"""
    count = db.query(models.TaskResult).filter(
        models.TaskResult.sid == sid,
        models.TaskResult.gid == gid,
    ).delete()
    db.commit()
    return count


def get_task_result(db: Session, sid: int, gid: int, tid: int) -> models.TaskResult | None:
    """获取某批次中某任务的最新结果。"""
    return (
        db.query(models.TaskResult)
        .filter(
            models.TaskResult.sid == sid,
            models.TaskResult.gid == gid,
            models.TaskResult.tid == tid,
        )
        .order_by(models.TaskResult.id.desc())
        .first()
    )
