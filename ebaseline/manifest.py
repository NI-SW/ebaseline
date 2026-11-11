"""批次冻结清单：创建批次时快照任务定义、组变量与脚本内容 hash。

目的：
- 历史批次详情始终展示当时的定义，不受后续任务编辑影响；
- 整批重跑使用冻结定义，避免“同一批次前后两次执行了不同任务”；
- 通过脚本 hash / 缺失标记说明当时实际能运行什么。

清单保存在 task_batch.manifest（JSON 文本）。旧批次没有清单时，
调用方回退到当前任务定义并明确标注。
"""
from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from . import models
from .config import get_config
from .security import mask_variables
from .task_graph import parse_depends

MANIFEST_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACES_ROOT = PROJECT_ROOT / "workspaces"

_INTERPRETERS = {"python", "python3", "bash", "sh"}


@dataclass
class FrozenTask:
    """从 manifest 还原的任务定义（字段与 models.TaskList 对齐）。"""

    tid: int
    gid: int
    name: str
    description: Optional[str]
    depends: Optional[str]
    pre_script: Optional[str]
    input: Optional[str]
    timeout: Optional[int]
    expect: Optional[str]
    clean: Optional[str]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_task(task: Any) -> dict:
    return {
        "tid": int(task.tid),
        "gid": int(task.gid),
        "name": task.name,
        "description": task.description,
        "depends": parse_depends(task.depends),
        "pre_script": task.pre_script,
        "input": task.input,
        "timeout": task.timeout,
        "expect": task.expect,
        "clean": task.clean,
    }


def task_signature(task: Any) -> str:
    payload = json.dumps(_canonical_task(task), ensure_ascii=False, sort_keys=True)
    return _sha256_bytes(payload.encode("utf-8"))


def _script_target(command: Optional[str], gid: int) -> dict:
    """解析脚本命令并计算 hash（不创建任何目录）。

    返回 {"command", "path", "sha256", "missing", "inline"}。
    """
    if not command or not str(command).strip():
        return {}
    command = str(command).strip()
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()

    inline_code: Optional[str] = None
    script_file: Optional[str] = None
    if parts and parts[0] in _INTERPRETERS and len(parts) > 1:
        if parts[1] == "-c":
            inline_code = " ".join(parts[2:])
        else:
            script_file = parts[1]
    elif parts:
        script_file = parts[0]

    info: dict[str, Any] = {"command": command, "inline": inline_code is not None}
    if inline_code is not None:
        info["sha256"] = _sha256_bytes(inline_code.encode("utf-8"))
        info["missing"] = False
        return info

    if not script_file:
        info["missing"] = True
        return info

    path = Path(script_file)
    if not path.is_absolute():
        ws_candidate = WORKSPACES_ROOT / f"group_{gid}" / script_file
        if ws_candidate.exists():
            path = ws_candidate
        else:
            path = get_config().scripts_base_dir / script_file
    info["path"] = str(path)
    try:
        if path.is_file():
            info["sha256"] = _sha256_bytes(path.read_bytes())
            info["missing"] = False
        else:
            info["missing"] = True
    except OSError as exc:
        info["missing"] = True
        info["error"] = str(exc)
    return info


def _task_entry(task: Any) -> dict:
    entry = _canonical_task(task)
    scripts = {}
    for field in ("pre_script", "expect", "clean"):
        target = _script_target(getattr(task, field, None), int(task.gid))
        if target:
            scripts[field] = target
    entry["scripts"] = scripts
    entry["signature"] = task_signature(task)
    return entry


def build_manifest(db: Session, sid: int, gid: int) -> dict:
    """构建批次清单（创建批次时调用）。"""
    subject = db.query(models.Subject).filter(models.Subject.id == sid).first()
    group = db.query(models.TaskGroup).filter(models.TaskGroup.id == gid).first()
    tasks = (
        db.query(models.TaskList)
        .filter(models.TaskList.gid == gid)
        .order_by(models.TaskList.tid.asc())
        .all()
    )

    variables: dict[str, Any] = {}
    if group and group.variables:
        try:
            parsed = json.loads(group.variables)
            if isinstance(parsed, dict):
                variables = parsed
        except (TypeError, ValueError):
            variables = {}

    return {
        "manifest_version": MANIFEST_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "sid": sid,
        "gid": gid,
        "subject": {
            "id": subject.id,
            "name": subject.name,
            "ipaddr": subject.ipaddr,
            "port": subject.port,
            "api_mode": subject.api_mode,
        } if subject else None,
        "group": {
            "id": group.id,
            "name": group.name,
            "python_version": group.python_version,
            "variables": variables,
        } if group else None,
        "tasks": [_task_entry(t) for t in tasks],
    }


def parse_manifest(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def tasks_from_manifest(manifest: dict) -> list[FrozenTask]:
    """把 manifest 还原为可执行任务列表；无清单返回空列表。"""
    out: list[FrozenTask] = []
    for item in manifest.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        try:
            depends = item.get("depends") or []
            depends_str = ",".join(str(int(x)) for x in depends) if depends else None
            out.append(FrozenTask(
                tid=int(item["tid"]),
                gid=int(item.get("gid", manifest.get("gid", 0))),
                name=str(item.get("name") or f"#{item['tid']}"),
                description=item.get("description"),
                depends=depends_str,
                pre_script=item.get("pre_script"),
                input=item.get("input"),
                timeout=item.get("timeout"),
                expect=item.get("expect"),
                clean=item.get("clean"),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def group_variables_from_manifest(manifest: dict) -> Optional[dict]:
    """返回冻结的组变量；清单不存在时返回 None（由调用方决定回退）。"""
    group = manifest.get("group")
    if isinstance(group, dict):
        variables = group.get("variables")
        if isinstance(variables, dict):
            return variables
    return None


def manifest_summary(manifest: dict, placeholder: str = "********") -> Optional[dict]:
    """面向 API 的清单摘要：脱敏变量，只列出脚本缺失项。"""
    if not manifest:
        return None
    tasks = manifest.get("tasks") or []
    missing: list[dict] = []
    for item in tasks:
        if not isinstance(item, dict):
            continue
        for field, script in (item.get("scripts") or {}).items():
            if isinstance(script, dict) and script.get("missing"):
                missing.append({
                    "tid": item.get("tid"),
                    "field": field,
                    "command": script.get("command"),
                    "path": script.get("path"),
                })
    group = manifest.get("group") or {}
    return {
        "manifest_version": manifest.get("manifest_version"),
        "created_at": manifest.get("created_at"),
        "task_count": len(tasks),
        "python_version": group.get("python_version") if isinstance(group, dict) else None,
        "variables": mask_variables(
            group.get("variables") if isinstance(group, dict) else {}, placeholder),
        "scripts_missing": missing,
        "subject": manifest.get("subject"),
    }


def manifest_task_signature(manifest: dict, tid: int) -> Optional[str]:
    for item in manifest.get("tasks") or []:
        if isinstance(item, dict) and int(item.get("tid", -1)) == int(tid):
            return item.get("signature")
    return None
