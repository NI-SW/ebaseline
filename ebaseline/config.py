"""配置加载模块：从 JSON 配置文件读取启动参数。

用法：
    python main.py -f env.conf
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_DEFAULT_CONFIG_PATH = Path(__file__).parent / "env.conf"


class ConfigError(RuntimeError):
    """配置加载或校验失败。"""


class Config:
    """启动配置。所有字段来自 JSON 文件，环境变量可覆盖部分值。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("EBASE_CONF") or _DEFAULT_CONFIG_PATH)
        if not self.path.exists():
            raise ConfigError(f"配置文件不存在: {self.path}")
        with open(self.path, "r", encoding="utf-8") as f:
            self._raw: dict[str, Any] = json.load(f)
        self._validate()

    def _validate(self) -> None:
        required = ["database", "server", "auth"]
        for section in required:
            if section not in self._raw:
                raise ConfigError(f"配置缺少必需节: {section}")
        db = self._raw["database"]
        for key in ("host", "port", "user", "password", "schema"):
            if key not in db:
                raise ConfigError(f"配置 database 缺少字段: {key}")

    # ── database ────────────────────────────────────────────────
    @property
    def db_host(self) -> str:
        return self._raw["database"]["host"]

    @property
    def db_port(self) -> int:
        return int(self._raw["database"]["port"])

    @property
    def db_user(self) -> str:
        return self._raw["database"]["user"]

    @property
    def db_password(self) -> str:
        return self._raw["database"]["password"]

    @property
    def db_schema(self) -> str:
        return self._raw["database"]["schema"]

    @property
    def db_url(self) -> str:
        from urllib.parse import quote_plus
        user = quote_plus(self.db_user)
        password = quote_plus(self.db_password)
        return (
            f"mysql+pymysql://{user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_schema}"
            "?charset=utf8mb4"
        )

    # ── server ──────────────────────────────────────────────────
    @property
    def server_host(self) -> str:
        return self._raw["server"].get("host", "0.0.0.0")

    @property
    def server_port(self) -> int:
        return int(self._raw["server"].get("port", 8080))

    @property
    def server_debug(self) -> bool:
        return bool(self._raw["server"].get("debug", False))

    # ── mcp ─────────────────────────────────────────────────────
    @property
    def mcp_enabled(self) -> bool:
        return bool(self._raw.get("mcp", {}).get("enabled", True))

    @property
    def mcp_host(self) -> str:
        return self._raw.get("mcp", {}).get("host", "0.0.0.0")

    @property
    def mcp_port(self) -> int:
        return int(self._raw.get("mcp", {}).get("port", 8081))

    @property
    def mcp_path(self) -> str:
        return self._raw.get("mcp", {}).get("path", "/mcp")

    @property
    def mcp_auth_token(self) -> str:
        """MCP 管理员 token；环境变量优先。"""
        return (
            os.environ.get("EBASE_MCP_TOKEN")
            or self._raw.get("mcp", {}).get("auth_token")
            or ""
        ).strip()

    @property
    def mcp_readonly_token(self) -> str:
        """MCP 只读 token；为空表示不启用只读角色。"""
        return (
            os.environ.get("EBASE_MCP_READONLY_TOKEN")
            or self._raw.get("mcp", {}).get("readonly_token")
            or ""
        ).strip()

    @property
    def mcp_token_file(self) -> Path:
        """自动生成 token 的持久化位置（相对配置文件目录）。"""
        p = Path(self._raw.get("mcp", {}).get("token_file", "runtime/mcp_token"))
        if not p.is_absolute():
            p = self.path.parent / p
        return p

    @property
    def mcp_allow_unauthenticated(self) -> bool:
        return bool(self._raw.get("mcp", {}).get("allow_unauthenticated", False))

    @property
    def mcp_allowed_hosts(self) -> list[str]:
        return list(self._raw.get("mcp", {}).get("allowed_hosts", []) or [])

    @property
    def mcp_allowed_origins(self) -> list[str]:
        return list(self._raw.get("mcp", {}).get("allowed_origins", []) or [])

    # ── workspace ───────────────────────────────────────────────
    @property
    def workspace_terminal_enabled(self) -> bool:
        return bool(self._raw.get("workspace", {}).get("terminal_enabled", True))

    @property
    def workspace_terminal_min_privilege(self) -> int:
        return int(self._raw.get("workspace", {}).get("terminal_min_privilege", 1))

    @property
    def workspace_allow_root_execution(self) -> bool:
        """服务以 root 运行时是否允许打开主机 shell。

        默认 false：root 服务下的 Web 终端会继承 root 身份，必须先显式放行
        或改用非 root 账号/隔离容器运行。
        """
        return bool(self._raw.get("workspace", {}).get("allow_root_execution", False))

    # ── security ────────────────────────────────────────────────
    @property
    def security_mask_group_secrets(self) -> bool:
        return bool(self._raw.get("security", {}).get("mask_group_secrets", True))

    @property
    def security_mask_placeholder(self) -> str:
        return str(self._raw.get("security", {}).get("mask_placeholder", "********"))

    # ── auth ────────────────────────────────────────────────────
    @property
    def auth_secret_key(self) -> str:
        key = self._raw["auth"].get("secret_key") or os.environ.get("EBASE_SECRET_KEY")
        if not key:
            raise ConfigError("auth.secret_key 未配置（也可设 EBASE_SECRET_KEY 环境变量）")
        return key

    @property
    def auth_allow_register(self) -> bool:
        return bool(self._raw.get("auth", {}).get("allow_register", True))

    @property
    def auth_allow_init_admin(self) -> bool:
        """是否允许通过公开接口创建初始 admin（默认关闭）。"""
        return bool(self._raw.get("auth", {}).get("allow_init_admin", False))

    @property
    def auth_initial_admin_password(self) -> str:
        """首次创建 admin 时的密码；为空则随机生成。"""
        return (
            os.environ.get("EBASE_ADMIN_PASSWORD")
            or self._raw.get("auth", {}).get("initial_admin_password")
            or ""
        )

    @property
    def auth_algorithm(self) -> str:
        return self._raw["auth"].get("algorithm", "HS256")

    @property
    def auth_token_expire_minutes(self) -> int:
        return int(self._raw["auth"].get("token_expire_minutes", 1440))

    # ── agent ───────────────────────────────────────────────────
    @property
    def agent_default_timeout(self) -> int:
        return int(self._raw.get("agent", {}).get("default_timeout", 300))

    @property
    def agent_max_follow_ups(self) -> int:
        return int(self._raw.get("agent", {}).get("max_follow_ups", 3))

    @property
    def agent_autonomy_directive(self) -> bool:
        return bool(self._raw.get("agent", {}).get("autonomy_directive", True))

    # ── scripts ─────────────────────────────────────────────────
    @property
    def scripts_base_dir(self) -> Path:
        p = Path(self._raw.get("scripts", {}).get("base_dir", "./scripts"))
        if not p.is_absolute():
            p = self.path.parent / p
        return p.resolve()

    @property
    def scripts_shell(self) -> str:
        return self._raw.get("scripts", {}).get("shell", "/bin/bash")


_config: Config | None = None


def load_config(path: str | Path | None = None) -> Config:
    """加载配置（单例）。重复调用返回同一实例。"""
    global _config
    if _config is None:
        _config = Config(path)
    return _config


def get_config() -> Config:
    """获取已加载的配置。未加载时抛 ConfigError。"""
    if _config is None:
        raise ConfigError("配置未加载，请先调用 load_config()")
    return _config
