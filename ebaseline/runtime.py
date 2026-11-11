"""运行上下文：任务在每个阶段的快照，供工作空间脚本通过 SDK 读取。

目录布局（在项目根目录 runtime/ 下，不暴露给工作空间文件 API）：

    runtime/ebase_runs/group_<gid>/batch_<bid>/attempt_<n>/<tid>.json    每任务一份上下文
    runtime/ebase_runs/group_<gid>/batch_<bid>/attempt_<n>/<tid>.qa.json QA 闭环记录（有则写）

旧版目录（无 attempt 层）仍然可读，新执行一律写入 attempt_<n>/。

同一批次内任务串行执行，不同批次目录隔离，因此无需加锁；
写入采用 tmp + os.replace，读取方（脚本）不会看到半截 JSON。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Optional

# 项目根目录 = ebaseline/ 包的上一级
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ROOT = PROJECT_ROOT / "runtime"

SCHEMA_VERSION = 1

# 单段文本上限（Agent 输出可能很大）：超出部分截断并打标记
MAX_TEXT_CHARS = 4_000_000

# 每组保留的批次目录数、每批次保留的 attempt 目录数（超出按修改时间淘汰）
KEEP_BATCH_DIRS = 20
KEEP_ATTEMPTS = 20


def batch_dir(gid: int, batch_id: int, attempt: Optional[int] = None) -> Path:
    """批次上下文目录。

    attempt 为 None 时返回旧版批次目录（兼容旧调用/旧数据）；
    指定 attempt 时返回 batch_<bid>/attempt_<n>/，重跑不再覆盖上一轮。
    """
    base = RUNTIME_ROOT / "ebase_runs" / f"group_{gid}" / f"batch_{batch_id}"
    if attempt is None:
        return base
    return base / f"attempt_{int(attempt)}"


def task_context_path(gid: int, batch_id: int, tid: int,
                      attempt: Optional[int] = None) -> Path:
    """单任务上下文文件路径。"""
    return batch_dir(gid, batch_id, attempt) / f"{tid}.json"


def qa_record_path(gid: int, batch_id: int, tid: int,
                   attempt: Optional[int] = None) -> Path:
    """QA 闭环记录文件路径。"""
    return batch_dir(gid, batch_id, attempt) / f"{tid}.qa.json"


def cap_text(text: Optional[str], limit: int = MAX_TEXT_CHARS) -> tuple[str, bool]:
    """截断超长文本，返回 (文本, 是否被截断)。"""
    if not text:
        return text or "", False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def write_context(path: Path, data: dict[str, Any]) -> Path:
    """原子写入上下文 JSON（tmp + os.replace）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def write_json(path: Path, data: Any) -> Path:
    """原子写入任意 JSON（QA 记录用）。"""
    return write_context(path, data)


def reset_batch(gid: int, batch_id: int, attempt: Optional[int] = None) -> None:
    """清空某批次（或某轮 attempt）的上下文目录。

    指定 attempt 时只清空该轮目录；上一轮的上下文保留，供历史排查。
    """
    shutil.rmtree(batch_dir(gid, batch_id, attempt), ignore_errors=True)


def prune_attempts(gid: int, batch_id: int, keep: int = KEEP_ATTEMPTS,
                   protected: Optional[set[int]] = None) -> None:
    """每批次保留最近 keep 个 attempt 目录。"""
    base = batch_dir(gid, batch_id)
    if not base.is_dir():
        return
    protected_names = {f"attempt_{n}" for n in (protected or ())}
    dirs = [
        d for d in base.iterdir()
        if d.is_dir() and d.name.startswith("attempt_") and d.name not in protected_names
    ]
    if len(dirs) <= keep:
        return
    dirs.sort(key=_last_activity, reverse=True)
    for d in dirs[keep:]:
        shutil.rmtree(d, ignore_errors=True)


def purge_group(gid: int) -> None:
    """删除某任务组的全部运行上下文（删组时调用）。"""
    shutil.rmtree(RUNTIME_ROOT / "ebase_runs" / f"group_{gid}", ignore_errors=True)


def _last_activity(directory: Path) -> float:
    """目录及其内文件的最新修改时间。

    目录 mtime 只在增删文件时变化，任务执行中不断改写上下文文件时不会更新，
    因此必须把文件 mtime 一并纳入，避免把正在执行的批次目录判定为“最旧”。
    """
    latest = directory.stat().st_mtime
    try:
        # attempt 目录内的上下文文件是持续改写的，必须递归查看文件 mtime
        for p in directory.rglob("*"):
            try:
                latest = max(latest, p.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        pass
    return latest


def prune_batches(gid: int, keep: int = KEEP_BATCH_DIRS,
                  protected: Optional[set[int]] = None) -> None:
    """按最近活动时间保留每组最近的 keep 个批次目录。

    protected 中的批次 id 不会删除（供执行器传入正在运行的批次）。
    """
    group_root = RUNTIME_ROOT / "ebase_runs" / f"group_{gid}"
    if not group_root.is_dir():
        return
    protected_names = {f"batch_{bid}" for bid in (protected or ())}
    dirs = [
        d for d in group_root.iterdir()
        if d.is_dir() and d.name.startswith("batch_") and d.name not in protected_names
    ]
    if len(dirs) <= keep:
        return
    dirs.sort(key=_last_activity, reverse=True)
    for d in dirs[keep:]:
        shutil.rmtree(d, ignore_errors=True)
