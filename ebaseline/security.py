"""敏感数据处理：组变量脱敏、编辑回填与日志脱敏。

组变量里可能直接保存数据库密码、API key 等。API/MCP 响应只下发占位符，
更新时把占位符合并回旧值，避免用户在页面上编辑非敏感字段时把秘密覆盖掉。
执行器仍然从数据库读取真实值注入脚本环境。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

SECRET_KEY_RE = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|apikey|credential|private[_-]?key)",
    re.IGNORECASE,
)

# 日志脱敏时忽略极短的值，避免把耗时/数字等正常文本替换得面目全非
_MIN_REDACT_LEN = 4


def is_secret_key(key: Any) -> bool:
    return isinstance(key, str) and bool(SECRET_KEY_RE.search(key))


def mask_variables(value: Any, placeholder: str = "********") -> Any:
    """递归复制并脱敏 dict/list 中的秘密字段。"""
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if is_secret_key(k) and v not in (None, ""):
                out[k] = placeholder
            else:
                out[k] = mask_variables(v, placeholder)
        return out
    if isinstance(value, list):
        return [mask_variables(v, placeholder) for v in value]
    return value


def merge_masked_values(old: Any, new: Any, placeholder: str = "********") -> Any:
    """更新时用旧值回填占位符。

    - 新值等于占位符：保留旧值；
    - 新旧都是 dict：逐 key 合并（新 dict 未出现的 key 视为删除）；
    - 其他情况以新值为准。
    """
    if isinstance(new, dict) and isinstance(old, dict):
        merged: dict[Any, Any] = {}
        for k, v in new.items():
            merged[k] = merge_masked_values(old.get(k), v, placeholder)
        return merged
    if new == placeholder and old is not None:
        return old
    return new


def collect_secret_values(value: Any, _out: set[str] | None = None) -> set[str]:
    """收集秘密字段的字符串值，用于日志脱敏。"""
    out = _out if _out is not None else set()
    if isinstance(value, dict):
        for k, v in value.items():
            if is_secret_key(k):
                _collect_scalars(v, out)
            else:
                collect_secret_values(v, out)
    elif isinstance(value, list):
        for v in value:
            collect_secret_values(v, out)
    return out


def _collect_scalars(value: Any, out: set[str]) -> None:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value)
        if len(text) >= _MIN_REDACT_LEN:
            out.add(text)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_scalars(v, out)
    elif isinstance(value, list):
        for v in value:
            _collect_scalars(v, out)


def redact_text(text: str, secrets: Iterable[str], placeholder: str = "********") -> str:
    """把文本中出现的秘密值替换为占位符（长值优先，避免部分覆盖）。"""
    if not text:
        return text
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret and secret != placeholder:
            text = text.replace(secret, placeholder)
    return text
