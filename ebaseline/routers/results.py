"""结果查询与导出路由。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..auth import get_current_user, require_privilege
from ..database import get_db

router = APIRouter(prefix="/api/v1/results", tags=["results"])


@router.get("", response_model=list[schemas.ResultResponse])
def list_results(
    sid: int | None = None,
    gid: int | None = None,
    tid: int | None = None,
    result: int | None = None,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """查询结果，支持按 sid/gid/tid/result 过滤。"""
    return crud.list_results(db, sid=sid, gid=gid, tid=tid, result=result)


@router.get("/export")
def export_results(
    sid: int | None = None,
    gid: int | None = None,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """导出结果为 CSV。"""
    results = crud.list_results(db, sid=sid, gid=gid)
    if not results:
        raise HTTPException(status_code=404, detail="没有可导出的结果")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "sid", "gid", "tid", "result", "process", "create_time"])
    for r in results:
        writer.writerow([
            r.id, r.sid, r.gid, r.tid, r.result,
            (r.process or "").replace("\n", "\\n"),
            r.create_time.isoformat() if r.create_time else "",
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=results.csv"},
    )


@router.get("/{result_id}")
def get_result(
    result_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """获取单条结果详情（含完整 process 日志）。"""
    r = crud.get_result(db, result_id)
    if r is None:
        raise HTTPException(status_code=404, detail="结果不存在")
    return {
        "id": r.id,
        "sid": r.sid,
        "gid": r.gid,
        "tid": r.tid,
        "result": r.result,
        "process": r.process,
        "create_time": r.create_time.isoformat() if r.create_time else None,
    }


@router.delete("", status_code=204)
def delete_results(
    ids: str,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(1)),
):
    """批量删除结果。ids 为逗号分隔的 id 列表，如 "1,2,3"。"""
    id_list = [int(x.strip()) for x in ids.split(",") if x.strip()]
    if not id_list:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    db.query(models.TaskResult).filter(models.TaskResult.id.in_(id_list)).delete(synchronize_session=False)
    db.commit()


@router.delete("/{result_id}", status_code=204)
def delete_result(
    result_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(1)),
):
    """删除单条结果（需管理员）。"""
    r = crud.get_result(db, result_id)
    if r is None:
        raise HTTPException(status_code=404, detail="结果不存在")
    db.delete(r)
    db.commit()
