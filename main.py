"""ebaseline 主入口：解析配置文件，启动 REST API 和 MCP 服务。

用法：
    python main.py -f env.conf
    ./main.py -f env.conf
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

# 项目根目录加入 sys.path，确保 ebaseline 包可导入
_PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(_PROJECT_ROOT))


def parse_args():
    parser = argparse.ArgumentParser(description="ebaseline - 智能体基线测试系统")
    parser.add_argument("-f", "--config", default="env.conf", help="配置文件路径（默认 env.conf）")
    return parser.parse_args()


def main():
    args = parse_args()

    from ebaseline.config import load_config
    from ebaseline.database import _get_session_factory
    from ebaseline.auth import init_admin_user

    cfg = load_config(args.config)

    print(f"[ebaseline] 配置文件: {cfg.path}")
    print(f"[ebaseline] 数据库: {cfg.db_user}@{cfg.db_host}:{cfg.db_port}/{cfg.db_schema}")
    print(f"[ebaseline] REST API: http://{cfg.server_host}:{cfg.server_port}")
    if cfg.mcp_enabled:
        print(f"[ebaseline] MCP:     http://{cfg.mcp_host}:{cfg.mcp_port}{cfg.mcp_path}")

    # 初始化 admin 用户
    db = _get_session_factory()()
    try:
        admin = init_admin_user(db)
        print(f"[ebaseline] admin 用户已就绪 (id={admin.id}, privilege={admin.privilege})")
    finally:
        db.close()

    # 启动 MCP 服务（后台线程）
    if cfg.mcp_enabled:
        from ebaseline.mcp_server import start_mcp_background
        start_mcp_background(cfg.mcp_host, cfg.mcp_port, cfg.mcp_path)
        print(f"[ebaseline] MCP 服务已在后台线程启动")

    # 启动 FastAPI（主线程）
    uvicorn.run(
        "main:app",
        host=cfg.server_host,
        port=cfg.server_port,
        reload=cfg.server_debug,
    )


# FastAPI app 实例（供 uvicorn 引用）
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from ebaseline.routers import auth, batches, groups, results, subjects, tasks, users, workspace

app = FastAPI(
    title="ebaseline",
    description="智能体基线测试系统",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(groups.router)
app.include_router(tasks.router)
app.include_router(subjects.router)
app.include_router(batches.router)
app.include_router(results.router)
app.include_router(workspace.router)


@app.get("/", include_in_schema=False)
def index():
    """Web 控制台首页。"""
    return FileResponse(str(_PROJECT_ROOT / "static" / "index.html"))


@app.get("/api")
def api_root():
    return {
        "name": "ebaseline",
        "version": "0.1.0",
        "docs": "/docs",
        "mcp": "/mcp",
    }


if __name__ == "__main__":
    main()
