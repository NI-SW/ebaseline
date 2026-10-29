"""任务组工作空间：文件管理 + Web 终端（WebSocket pty）。

每个任务组有独立工作空间目录 workspaces/group_<gid>/：
  - 存放该组的脚本及其依赖
  - 执行引擎运行脚本时优先在此目录解析
  - 用户可通过 Web 界面浏览/编辑文件、打开 Linux 终端测试脚本
"""
from __future__ import annotations

import os
import pty
import select
import signal
import struct
import subprocess
import termios
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import crud, models
from ..auth import require_privilege, decode_token
from ..database import get_db

router = APIRouter(prefix="/api/v1/groups/{gid}/workspace", tags=["workspace"])

# 项目根目录 = ebaseline/ 包的上一级（main.py 所在目录）
WORKSPACES_ROOT = Path(__file__).resolve().parent.parent.parent / "workspaces"

# 可用的 Python 解释器注册表（版本号 → 可执行文件路径）
PYTHON_INTERPRETERS = {
    "3.9": "/usr/bin/python3.9",
    "3.11": "/root/.local/bin/python3.11",
    "3.14": "/usr/bin/python3.14",
}


def _workspace_dir(gid: int) -> Path:
    """返回组工作空间目录（不存在则创建）。"""
    d = WORKSPACES_ROOT / f"group_{gid}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _venv_dir(gid: int) -> Path:
    """组独立 venv 目录。"""
    return _workspace_dir(gid) / ".venv"


def _venv_python(gid: int) -> Path:
    """组独立 venv 的 python 可执行文件路径。"""
    return _venv_dir(gid) / "bin" / "python"


def _ensure_venv(gid: int, python_version: str) -> Path:
    """确保组 venv 存在（懒创建），返回 python 路径。

    创建失败时回退到项目 venv，保证功能可用。
    """
    venv_py = _venv_python(gid)
    if venv_py.exists():
        return venv_py

    interpreter = PYTHON_INTERPRETERS.get(python_version)
    if interpreter is None or not Path(interpreter).exists():
        # 版本不可用，回退到项目 venv
        fallback = Path(__file__).resolve().parent.parent.parent / "venv" / "bin" / "python"
        return fallback

    # 创建 venv（含 pip）
    import subprocess
    result = subprocess.run(
        [interpreter, "-m", "venv", str(_venv_dir(gid))],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        fallback = Path(__file__).resolve().parent.parent.parent / "venv" / "bin" / "python"
        return fallback

    # 升级 pip 并装基础包
    pip = str(_venv_dir(gid) / "bin" / "pip")
    subprocess.run([pip, "install", "--quiet", "--upgrade", "pip"], capture_output=True, timeout=120)
    subprocess.run([pip, "install", "--quiet", "requests"], capture_output=True, timeout=120)
    return venv_py


def _group_python(gid: int, db: Session) -> Path:
    """获取组工作空间的 python 路径（带 venv 懒创建）。"""
    group = crud.get_task_group(db, gid)
    version = group.python_version if group else "3.11"
    return _ensure_venv(gid, version)


def _safe_resolve(gid: int, rel_path: str) -> Path:
    """将相对路径解析为工作空间内的绝对路径，拒绝路径穿越。"""
    base = _workspace_dir(gid)
    # 去掉开头的 /，防止被当作绝对路径
    rel = rel_path.lstrip("/")
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        raise HTTPException(status_code=400, detail=f"非法路径（越出工作空间）: {rel_path}")
    return target


def _check_group(db: Session, gid: int) -> models.TaskGroup:
    group = crud.get_task_group(db, gid)
    if group is None:
        raise HTTPException(status_code=404, detail="任务组不存在")
    return group


# ── 文件 API ──────────────────────────────────────────────────

class FileWriteRequest(BaseModel):
    content: str


class MkdirRequest(BaseModel):
    path: str


@router.get("/files")
def list_files(
    gid: int,
    path: str = "",
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(0)),
):
    """列出工作空间目录内容。"""
    _check_group(db, gid)
    target = _safe_resolve(gid, path or ".")
    if not target.exists():
        raise HTTPException(status_code=404, detail="路径不存在")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="不是目录")

    base = _workspace_dir(gid)
    entries = []
    for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
        st = item.stat()
        entries.append({
            "name": item.name,
            "path": str(item.relative_to(base)),
            "is_dir": item.is_dir(),
            "size": st.st_size if item.is_file() else None,
            "mtime": st.st_mtime,
        })
    return {"path": str(target.relative_to(base)), "entries": entries}


@router.get("/file")
def read_file(
    gid: int,
    path: str = Query(...),
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(0)),
):
    """读取文件内容（限制 2MB）。"""
    _check_group(db, gid)
    target = _safe_resolve(gid, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    if target.stat().st_size > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大（>2MB），不支持在线编辑")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="二进制文件不支持在线查看")
    return {"path": path, "content": content}


@router.put("/file")
def write_file(
    gid: int,
    data: FileWriteRequest,
    path: str = Query(...),
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(1)),
):
    """写入文件（不存在则创建，父目录自动创建）。"""
    _check_group(db, gid)
    target = _safe_resolve(gid, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(data.content, encoding="utf-8")
    # 脚本文件自动加可执行权限
    if target.suffix in (".py", ".sh", ".bash"):
        target.chmod(target.stat().st_mode | 0o111)
    return {"path": path, "size": target.stat().st_size}


@router.post("/mkdir")
def make_dir(
    gid: int,
    data: MkdirRequest,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(1)),
):
    """创建目录。"""
    _check_group(db, gid)
    target = _safe_resolve(gid, data.path)
    target.mkdir(parents=True, exist_ok=True)
    return {"path": data.path}


@router.delete("/file")
def delete_file(
    gid: int,
    path: str = Query(...),
    db: Session = Depends(get_db),
    _: models.User = Depends(require_privilege(1)),
):
    """删除文件或目录（目录递归删除）。"""
    _check_group(db, gid)
    target = _safe_resolve(gid, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="路径不存在")
    base = _workspace_dir(gid)
    if target == base:
        raise HTTPException(status_code=400, detail="不能删除工作空间根目录")
    if target.is_dir():
        import shutil
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"deleted": path}


# ── Web 终端（WebSocket + pty）────────────────────────────────

@router.websocket("/terminal")
async def terminal_ws(websocket: WebSocket, gid: int, token: str = Query(...)):
    """Linux 终端。认证通过 query 参数 token（WebSocket 不方便带 header）。

    终端 cwd 固定为该组工作空间目录，PATH 注入项目 venv 的 python。
    """
    # 认证
    try:
        payload = decode_token(token)
        user_id = int(payload.get("sub", 0))
    except Exception:
        await websocket.close(code=4401)
        return

    db = next(get_db())
    try:
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if user is None:
            await websocket.close(code=4401)
            return
        if crud.get_task_group(db, gid) is None:
            await websocket.close(code=4404)
            return
    finally:
        db.close()

    await websocket.accept()
    ws_dir = _workspace_dir(gid)

    # 准备环境：PATH 前置组独立 venv（懒创建），注入 EBASE_GID
    # 重新开一个会话查 python_version（上面的 db 已关闭）
    from ..database import _get_session_factory
    db2 = _get_session_factory()()
    try:
        group_obj = crud.get_task_group(db2, gid)
        py_version = group_obj.python_version if group_obj else "3.11"
    finally:
        db2.close()
    venv_py = _ensure_venv(gid, py_version)
    venv_bin = str(venv_py.parent)

    env = os.environ.copy()
    env["PATH"] = f"{venv_bin}:" + env.get("PATH", "")
    env["EBASE_GID"] = str(gid)
    env["EBASE_WORKSPACE"] = str(ws_dir)
    env["TERM"] = "xterm-256color"
    env["PS1"] = f"\\[\\e[32m\\]group-{gid}\\[\\e[0m\\]:\\[\\e[34m\\]\\w\\[\\e[0m\\]$ "

    # fork pty
    pid, fd = pty.fork()
    if pid == 0:
        # 子进程
        os.chdir(str(ws_dir))
        os.execvpe("/bin/bash", ["/bin/bash", "--norc", "-i"], env)

    # 父进程：设置终端大小（默认 80x24）
    try:
        import fcntl
        winsz = struct.pack("HHHH", 24, 120, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsz)
    except Exception:
        pass

    import asyncio
    loop = asyncio.get_event_loop()
    closed = False

    def _cleanup():
        nonlocal closed
        if closed:
            return
        closed = True
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGHUP)
        except (ProcessLookupError, OSError):
            pass

    async def pty_to_ws():
        """pty 输出 → websocket"""
        try:
            while not closed:
                r, _, _ = await loop.run_in_executor(None, select.select, [fd], [], [], 0.1)
                if fd in r:
                    try:
                        data = os.read(fd, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    await websocket.send_bytes(data)
        except Exception:
            pass
        finally:
            _cleanup()

    async def ws_to_pty():
        """websocket 输入 → pty"""
        try:
            while not closed:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                data = msg.get("bytes")
                if data:
                    os.write(fd, data)
                else:
                    text = msg.get("text")
                    if text:
                        # 控制消息：{"type":"resize","rows":R,"cols":C}
                        import json as _json
                        try:
                            ctrl = _json.loads(text)
                            if ctrl.get("type") == "resize":
                                import fcntl
                                winsz = struct.pack(
                                    "HHHH", int(ctrl["rows"]), int(ctrl["cols"]), 0, 0)
                                fcntl.ioctl(fd, termios.TIOCSWINSZ, winsz)
                                continue
                        except (ValueError, KeyError):
                            pass
                        os.write(fd, text.encode())
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            _cleanup()

    try:
        await asyncio.gather(pty_to_ws(), ws_to_pty())
    finally:
        _cleanup()
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
