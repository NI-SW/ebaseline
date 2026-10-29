#!/usr/bin/env python3
"""i2up 控制台 MCP (up-console-mcp) 共用客户端库。

test/ 下所有检查脚本与注入脚本通过本库操作 i2UP 控制台，
替代已删除的 i2stream-rule-manager / i2stream-db-manager SDK 脚本。

仅依赖标准库（底层用 curl 走 streamable HTTP JSON-RPC），
因为 check_commands 以裸 `python` 执行，不经过项目 venv。

连接地址解析优先级: 环境变量 I2UP_CONSOLE_MCP_URL > test/env.conf 的 CONSOLE_MCP_URL > 内置默认值。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MCP_URL = "http://192.168.50.19:58080/mcp"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_MCP_URL_CACHE = None


def _env_conf() -> dict:
    """读取 test/env.conf（解析失败返回空 dict）。"""
    conf = TEST_DIR / "env.conf"
    try:
        return json.loads(conf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def get_mcp_url() -> str:
    global _MCP_URL_CACHE
    if _MCP_URL_CACHE:
        return _MCP_URL_CACHE
    url = os.environ.get("I2UP_CONSOLE_MCP_URL") or _env_conf().get("CONSOLE_MCP_URL") or DEFAULT_MCP_URL
    _MCP_URL_CACHE = url.rstrip("/")
    return _MCP_URL_CACHE


class ConsoleMCPError(RuntimeError):
    """控制台 MCP 调用失败。"""


# ── i2up AK/SK 签名直连调用（仅用于 MCP 二进制无法正确发送 body 的 DELETE）──────
# up_mcp_server 的 DELETE 实现不携带请求体（已用本地回包拦截实测），
# 而控制台 DELETE /vers/v3/sync_rule 必须从 body 读取 rule_uuids，
# 因此删除规则改为按 info2soft SDK 同款算法直接签名调用控制台 REST API。

def get_console_api_base() -> str:
    url = (os.environ.get("I2UP_CONSOLE_API_BASE")
           or _env_conf().get("CONSOLE_API_BASE")
           or "https://192.168.50.19:58086/stream/api")
    return url.rstrip("/")


def get_console_credentials() -> tuple[str, str]:
    conf = _env_conf()
    ak = os.environ.get("I2UP_CONSOLE_ACCESS_KEY") or conf.get("CONSOLE_ACCESS_KEY") or ""
    sk = os.environ.get("I2UP_CONSOLE_SECRET_KEY") or conf.get("CONSOLE_SECRET_KEY") or ""
    if not ak or not sk:
        raise ConsoleMCPError(
            "缺少控制台 AK/SK（env.conf 的 CONSOLE_ACCESS_KEY/CONSOLE_SECRET_KEY）")
    return ak, sk


def _cache_buster() -> str:
    import random
    import struct
    return hex(struct.unpack('<I', struct.pack('<f', random.random()))[0])[2:]


def _i2up_sign_headers(method: str, url: str, body: dict, ak: str, sk: str) -> dict:
    """复刻 info2soft SDK https._generate_header 的 AK/SK 签名。"""
    import hashlib
    import hmac
    import time
    import urllib.parse
    import uuid as _uuid

    timestamp = str(int(round(time.time() * 1000)) / 1000)
    nonce = str(_uuid.uuid4())
    parsed = urllib.parse.urlsplit(url)
    api_pos = parsed.path.find("/api")
    uri = parsed.path[api_pos:] if api_pos != -1 else parsed.path

    sign_str = f"{method.upper()}\n{uri}\n{body.get('_', '')}\n{timestamp}\n{nonce}"
    signature = hmac.new(sk.encode(), sign_str.encode(), hashlib.sha256).hexdigest()

    # enhance 签名：对 body 字段按 key 排序拼接后做第二次 HMAC
    sign_fields = []
    for k in sorted(body):
        v = body[k]
        if v == "" or v is None:
            continue
        if not isinstance(v, str):
            original_is_container = isinstance(v, (dict, list))
            v = json.dumps(v, separators=(",", ":"), ensure_ascii=False).replace('"', "")
            if original_is_container:
                v = v.replace("{}", "[]")
        sign_fields.append(f"{k}={v}")
    enhance = "&".join(sign_fields).replace('"', "")
    enhance_sig = hmac.new(sk.encode(), enhance.encode(), hashlib.sha256).hexdigest()

    return {
        "User-Agent": "i2stream-test-harness/1.0",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "ACCESS-KEY": ak,
        "timestamp": timestamp,
        "nonce": nonce,
        "Signature": signature.lower(),
        "enhanceStr": enhance_sig.lower(),
    }


def console_direct_delete(rule_uuids: list[str], force: bool = False) -> dict:
    """直连控制台 DELETE /vers/v3/sync_rule（带 JSON body）。"""
    import ssl
    import urllib.request

    ak, sk = get_console_credentials()
    url = f"{get_console_api_base()}/vers/v3/sync_rule"
    body = {"rule_uuids": rule_uuids, "_": _cache_buster()}
    if force:
        body["force"] = 1
    headers = _i2up_sign_headers("DELETE", url, body, ak, sk)
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="DELETE")
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
        ret = json.loads(resp.read().decode("utf-8"))
    ensure_console_ok(ret, "console_direct_delete")
    return ret


def console_direct_delete_db(uuids: list[str], force: bool = False) -> dict:
    """直连控制台 DELETE /vers/v3/active/db（带 JSON body）。

    db 节点删除走 REST 直连：MCP 的 delete_db 工具发 DELETE 请求无法携带 body，
    会报 1010001051；此通道经 DB2 删除用例实测可用。force=1 用于节点 ABNORMAL 时强制删。
    """
    import ssl
    import urllib.request

    ak, sk = get_console_credentials()
    url = f"{get_console_api_base()}/vers/v3/active/db"
    body = {"uuids": uuids, "_": _cache_buster()}
    if force:
        body["force"] = 1
    headers = _i2up_sign_headers("DELETE", url, body, ak, sk)
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="DELETE")
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
        ret = json.loads(resp.read().decode("utf-8"))
    ensure_console_ok(ret, "console_direct_delete_db")
    return ret


class ConsoleMCP:
    """up-console-mcp 的最小 JSON-RPC 客户端（streamable HTTP）。"""

    def __init__(self, url: str | None = None, timeout: int = 180):
        self.url = (url or get_mcp_url()).rstrip("/")
        self.timeout = timeout
        self._id = 0

    # ── JSON-RPC 传输 ────────────────────────────────────────────────
    def _raw_post(self, payload: dict, timeout: int) -> str:
        r = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout), "-X", "POST", self.url,
             "-H", "Content-Type: application/json",
             "-H", "Accept: application/json, text/event-stream",
             "-d", json.dumps(payload, ensure_ascii=False)],
            capture_output=True, text=True)
        if r.returncode != 0 and not r.stdout:
            raise ConsoleMCPError(f"控制台 MCP 请求失败: {r.stderr.strip()[:300]}")
        return r.stdout

    @staticmethod
    def _extract(out: str, rpc_id: int) -> dict:
        out = out.strip()
        if out.startswith("{"):
            candidates = [json.loads(out)]
        else:
            candidates = []
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    try:
                        candidates.append(json.loads(line[5:].strip()))
                    except json.JSONDecodeError:
                        continue
        for cand in candidates:
            if isinstance(cand, dict) and cand.get("id") == rpc_id:
                return cand
        return {}

    def _rpc(self, method: str, params: dict | None = None, timeout: int | None = None) -> dict:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        out = self._raw_post(payload, timeout or self.timeout)
        data = self._extract(out, self._id)
        if not data:
            raise ConsoleMCPError(f"控制台 MCP 无响应 (method={method}): {out[:300]}")
        if "error" in data:
            raise ConsoleMCPError(f"控制台 MCP RPC 错误 (method={method}): {data['error']}")
        return data.get("result", {})

    # ── MCP 层 ──────────────────────────────────────────────────────
    def list_tools(self) -> list[dict]:
        """tools/list（自动分页，服务端单页上限 1000）。"""
        tools, cursor = [], None
        while True:
            params = {} if cursor is None else {"cursor": cursor}
            result = self._rpc("tools/list", params)
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call(self, name: str, arguments: dict | None = None, timeout: int | None = None) -> dict:
        """tools/call，返回 {"ok": bool, "data": 解析后的 JSON 或原始文本}。"""
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}}, timeout)
        is_err = result.get("isError", False)
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        raw = "\n".join(texts)
        # 工具实现可能把 HTTP 错误包在正常输出里（isError=false 但内容为 API error）
        if not is_err and raw.startswith("API error"):
            raise ConsoleMCPError(f"{name}: {raw[:300]}")
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            parsed = raw
        return {"ok": not is_err, "data": parsed}


# ── schema 一致性清洗 ────────────────────────────────────────────────────
# 控制台返回的数据与 MCP 入参 schema 存在类型出入（如 null vs object、
# [] vs object），提交前按 inputSchema 丢弃不符合类型的键，避免参数校验失败。

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def sanitize_for_schema(body: dict, schema: dict) -> tuple[dict, list[str]]:
    """按 inputSchema 清洗 body：类型不符的键被丢弃。返回 (clean, dropped_keys)。"""
    props = schema.get("properties") or {}
    clean, dropped = {}, []
    for key, value in body.items():
        prop = props.get(key)
        if prop is None:
            continue  # 未声明字段一律不发送
        expected = prop.get("type")
        check = _TYPE_CHECKS.get(expected)
        if check is not None and not check(value):
            dropped.append(key)
            continue
        clean[key] = value
    for key in schema.get("required") or []:
        if key not in clean:
            clean[key] = 0 if schema["properties"].get(key, {}).get("type") in ("number", "integer") else ""
    return clean, dropped


def ensure_console_ok(ret: dict, what: str) -> dict:
    """校验控制台 REST 返回（{ret, msg, data}），失败抛 ConsoleMCPError。"""
    if not isinstance(ret, dict):
        raise ConsoleMCPError(f"{what}: 响应不是对象: {str(ret)[:300]}")
    if ret.get("ret") not in (None, 200):
        raise ConsoleMCPError(f"{what}: ret={ret.get('ret')} msg={ret.get('msg')}")
    data = ret.get("data")
    if isinstance(data, dict) and data.get("code") not in (None, 0):
        raise ConsoleMCPError(f"{what}: code={data.get('code')} message={data.get('message')}")
    return data if isinstance(data, dict) else {}


# ── 摘要构造（保持与旧 query_i2stream_sync_rule_info.py 的 summary 键兼容）────

def _phase(item: dict, key: str) -> dict:
    section = (item.get("state") or {}).get(key) or {}
    return {"status": section.get("status"), "error_code": section.get("error_code")}


def rule_summary(item: dict) -> dict:
    """把 list_sync_rules 列表项转换成旧版 summary 结构。"""
    return {
        "rule_name": item.get("rule_name"),
        "rule_uuid": item.get("rule_uuid"),
        "source": {
            "name": item.get("src_db_name"),
            "type": item.get("src_db_type"),
            "uuid": item.get("src_db_uuid"),
            "node_name": item.get("src_node_name"),
            "node_ip": item.get("src_node_ip"),
        },
        "target": {
            "name": item.get("tgt_db_name"),
            "type": item.get("tgt_db_type"),
            "uuid": item.get("tgt_db_uuid"),
            "node_name": item.get("tgt_node_name"),
            "node_ip": item.get("tgt_node_ip"),
        },
        "rule_status": item.get("status"),
        "stage": (item.get("state") or {}).get("stage"),
        "export": _phase(item, "dump_state"),
        "import": _phase(item, "import_state"),
        "parse": _phase(item, "track_state"),
        "load": _phase(item, "load_state"),
        "delay": (item.get("state") or {}).get("delay"),
        "has_new_error": (item.get("state") or {}).get("has_new_err"),
        "full_sync_enabled": (item.get("full_sync_settings") or {}).get("full_sync"),
        "incremental_sync_enabled": (item.get("inc_sync_settings") or {}).get("incre_sync"),
    }


# ── 常用高层操作 ─────────────────────────────────────────────────────────

class Console:
    """面向测试脚本的高层封装。"""

    def __init__(self, mcp: ConsoleMCP | None = None):
        self.mcp = mcp or ConsoleMCP()
        self._tool_schemas: dict[str, dict] | None = None

    def tool_schema(self, name: str) -> dict:
        """取工具 inputSchema（带缓存）。"""
        if self._tool_schemas is None:
            self._tool_schemas = {t["name"]: t.get("inputSchema", {})
                                  for t in self.mcp.list_tools()}
        return self._tool_schemas.get(name, {})

    # ── 同步规则 ────────────────────────────────────────────────
    def list_rules(self, search_value: str = "", limit: int = 500) -> list[dict]:
        ret = self.mcp.call("list_sync_rules", {
            "page": 1, "limit": limit,
            "search_field": "rule_name", "search_value": search_value,
        })["data"]
        data = ensure_console_ok(ret, "list_sync_rules")
        return data.get("info_list") or []

    def find_rule(self, rule_name: str) -> dict | None:
        """按名称精确查找规则；不存在返回 None。"""
        for item in self.list_rules(search_value=rule_name):
            if item.get("rule_name") == rule_name and item.get("rule_uuid"):
                return item
        return None

    def resolve_rule(self, rule_name: str) -> dict:
        item = self.find_rule(rule_name)
        if item is None:
            raise ConsoleMCPError(f"规则名不存在: {rule_name}")
        return item

    def get_rule(self, rule_name: str) -> dict | None:
        """返回规则摘要（旧 summary 结构），不存在返回 None。"""
        item = self.find_rule(rule_name)
        return rule_summary(item) if item else None

    def describe_rule(self, rule_name: str) -> dict:
        """返回规则完整配置 (data.sync_rule)。"""
        item = self.resolve_rule(rule_name)
        ret = self.mcp.call("describe_sync_rules", {"uuid": item["rule_uuid"]})["data"]
        data = ensure_console_ok(ret, f"describe_sync_rules({rule_name})")
        sync_rule = data.get("sync_rule")
        if not isinstance(sync_rule, dict):
            raise ConsoleMCPError(f"规则详情缺少 sync_rule: {rule_name}")
        return sync_rule

    def operate_rule(self, rule_name: str, operate: str, **extra) -> dict:
        """start→resume / stop(+stop-parse) / restart 等规则操作。"""
        item = self.resolve_rule(rule_name)
        arguments = {"operate": operate, "rule_uuids": [item["rule_uuid"]]}
        arguments.update(extra)
        ret = self.mcp.call("post_vers_v3_sync_rule_operate", arguments)["data"]
        ensure_console_ok(ret, f"operate_rule({rule_name}, {operate})")
        return ret

    def delete_rule(self, rule_name: str, force: bool = False) -> dict:
        """删除规则。

        up_mcp_server 的 DELETE 不发送请求体（二进制缺陷），控制台收不到
        rule_uuids 会报 1010001051，因此这里直接签名调用控制台 REST API。
        """
        item = self.resolve_rule(rule_name)
        ret = console_direct_delete([item["rule_uuid"]], force=force)
        return ret

    def create_sync_rule(self, sync_rule: dict) -> str:
        """创建规则，返回新规则 uuid。"""
        ret = self.mcp.call("create_sync_rule", sync_rule, timeout=300)["data"]
        data = ensure_console_ok(ret, "create_sync_rule")
        uuid = data.get("uuid")
        if not uuid:
            raise ConsoleMCPError(f"create_sync_rule 未返回 uuid: {json.dumps(data, ensure_ascii=False)[:300]}")
        return uuid

    def modify_sync_rule(self, rule_name: str, body: dict) -> dict:
        """单规则修改（PUT /vers/v3/sync_rule，body 为完整规则配置 + active_flag）。

        优先使用 modify_sync_rule 工具（endpoints.json 补充的单规则修改端点）；
        若 MCP 未暴露则回退 batch_modify_sync_rule。控制台返回数据与入参
        schema 有类型出入，提交前按 inputSchema 清洗。
        """
        item = self.resolve_rule(rule_name)
        arguments = dict(body)
        arguments["active_flag"] = "modify"
        tool = "modify_sync_rule" if self.tool_schema("modify_sync_rule") else "batch_modify_sync_rule"
        if tool == "batch_modify_sync_rule":
            arguments["rule_uuids"] = [item["rule_uuid"]]
            arguments.pop("active_flag", None)
        clean, dropped = sanitize_for_schema(arguments, self.tool_schema(tool))
        if dropped:
            print(f"[modify_sync_rule] 已按 schema 丢弃类型不符字段: {', '.join(dropped)}")
        ret = self.mcp.call(tool, clean, timeout=300)["data"]
        ensure_console_ok(ret, f"modify_sync_rule({rule_name})")
        return ret

    def wait_rule_status(self, rule_name: str, predicate, timeout: int = 300,
                         interval: int = 10) -> dict | None:
        """轮询规则直到 predicate(summary) 为真，超时返回最后摘要。"""
        deadline = time.time() + timeout
        summary = None
        while time.time() < deadline:
            summary = self.get_rule(rule_name)
            if summary is not None:
                try:
                    if predicate(summary):
                        return summary
                except Exception:
                    pass
            time.sleep(interval)
        return summary

    # ── 数据库节点 ──────────────────────────────────────────────
    def list_dbs(self, search_value: str = "", limit: int = 300) -> list[dict]:
        ret = self.mcp.call("list_dbs", {
            "page": 1, "limit": limit,
            "search_field": "db_name", "search_value": search_value,
        })["data"]
        data = ensure_console_ok(ret, "list_dbs")
        return data.get("info_list") or []

    def find_db(self, db_name: str) -> dict | None:
        for item in self.list_dbs(search_value=db_name):
            if item.get("db_name") == db_name and item.get("db_uuid"):
                return item
        return None

    def db_status(self, db_uuids: list[str]) -> list[dict]:
        ret = self.mcp.call("list_db_status", {"uuids": db_uuids})["data"]
        data = ensure_console_ok(ret, "list_db_status")
        return data.get("status") or []

    def delete_db_by_prefix(self, prefix: str, force: bool = True) -> list[dict]:
        """删除所有 db_name 以 prefix 开头的节点，返回被删除节点清单（测试清理用）。

        走 REST 直连删除（console_direct_delete_db），force 默认 True 以便连
        ABNORMAL 节点也能清掉。返回 [{"db_name","db_uuid"}, ...]。
        """
        targets = [
            {"db_name": item.get("db_name"), "db_uuid": item.get("db_uuid")}
            for item in self.list_dbs(search_value=prefix)
            if str(item.get("db_name", "")).startswith(prefix) and item.get("db_uuid")
        ]
        if not targets:
            return []
        uuids = [t["db_uuid"] for t in targets]
        console_direct_delete_db(uuids, force=force)
        return targets

    # ── 表比较 ──────────────────────────────────────────────────
    def list_tb_cmp(self) -> list[dict]:
        ret = self.mcp.call("list_sync_tb_cmp", {})["data"]
        data = ensure_console_ok(ret, "list_sync_tb_cmp")
        return data.get("info_list") or []

    def find_tb_cmp(self, cmp_name: str) -> dict | None:
        for item in self.list_tb_cmp():
            if item.get("tb_cmp_name") == cmp_name and item.get("tb_cmp_uuid"):
                return item
        return None


# ── DM 同步规则 payload 构造（复刻旧 rules/dm.py + rules/mysql.py 行为）──────

SYNC_TYPE_STRUCTURE_MIGRATION = 0
SYNC_TYPE_FULL_SYNC = 1
SYNC_TYPE_INCREMENTAL_SYNC = 2
YES_NO_TO_INT = {"yes": 1, "no": 0}


def load_dm_template(template_path: str | None = None) -> dict:
    path = Path(template_path) if template_path else TEMPLATE_DIR / "dm_to_other.example.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _set_sync_type(sync_rule: dict, sync_type: int, enabled: str | None) -> None:
    if enabled is None:
        return
    if enabled not in YES_NO_TO_INT:
        raise ValueError(f"sync stage switch must be yes or no, got: {enabled}")
    current = sync_rule.setdefault("sync_type", [])
    if enabled == "yes" and sync_type not in current:
        current.append(sync_type)
    elif enabled == "no" and sync_type in current:
        current.remove(sync_type)


def build_dm_sync_rule(
    template: dict,
    *,
    rule_name: str,
    sync_scope: str,
    source_db_uuid: str,
    target_db_uuid: str,
    source_db_type: str = "dm",
    target_db_type: str | None = None,
    source_db_type_uuid: str | None = None,
    source_schema: str | None = None,
    target_schema: str | None = None,
    enable_structure_migration: str | None = None,
    enable_full_sync: str | None = None,
    enable_incremental_sync: str | None = None,
    start_rule_now: str | None = None,
    trackmode: str | None = None,
) -> dict:
    """按旧 rule_create.py dm 子命令的语义构造 create_sync_rule 入参。"""
    import copy

    if sync_scope not in ("db", "user", "table"):
        raise ValueError(f"--sync-scope 必须是 db/user/table，得到: {sync_scope}")
    payload = copy.deepcopy(template)
    sync_rule = payload["sync_rule"]

    sync_rule["rule_name"] = rule_name
    sync_rule["src_db_uuid"] = source_db_uuid
    sync_rule["tgt_db_uuid"] = target_db_uuid
    if source_db_type is not None:
        sync_rule["src_db_type"] = source_db_type
    if source_db_type_uuid:
        sync_rule["src_db_type_uuid"] = source_db_type_uuid
    if target_db_type:
        sync_rule["tgt_db_type"] = target_db_type
        db_set = sync_rule.get("base_settings", {}).get("db_set")
        if isinstance(db_set, list) and db_set:
            db_set[0]["tgt_db_type"] = target_db_type

    sync_rule["map_type"] = sync_scope
    sync_rule["map_type_list"] = [] if sync_scope == "db" else [sync_scope]
    sync_rule["database_map"] = []
    # MCP 入参 schema 声明 user_map/table_map 为 Array，空映射用 [] 表达
    sync_rule["user_map"] = []
    sync_rule["table_map"] = []

    # MCP 入参 schema 将 convert_urp_of_key 设为必填（模板中仅在 inc_sync_settings 内）
    if "convert_urp_of_key" not in sync_rule:
        sync_rule["convert_urp_of_key"] = sync_rule.get("inc_sync_settings", {}).get(
            "convert_urp_of_key", 0)

    _set_sync_type(sync_rule, SYNC_TYPE_STRUCTURE_MIGRATION, enable_structure_migration)
    _set_sync_type(sync_rule, SYNC_TYPE_FULL_SYNC, enable_full_sync)
    _set_sync_type(sync_rule, SYNC_TYPE_INCREMENTAL_SYNC, enable_incremental_sync)

    full_settings = sync_rule["full_sync_settings"]
    inc_settings = sync_rule["inc_sync_settings"]
    if enable_full_sync is not None:
        full_settings["full_sync"] = YES_NO_TO_INT[enable_full_sync]
    if enable_incremental_sync is not None:
        inc_settings["incre_sync"] = YES_NO_TO_INT[enable_incremental_sync]
    if start_rule_now is not None:
        sync_rule["base_settings"]["start_rule_now"] = YES_NO_TO_INT[start_rule_now]
    if trackmode is not None:
        inc_settings["trackmode"] = trackmode

    if sync_scope == "user":
        if not source_schema or not target_schema:
            raise ValueError("--sync-scope user 需要 --source-schema/--target-schema")
        sync_rule["user_map"] = [{"src_user": source_schema, "tgt_user": target_schema}]
        # Web 控制台对 DM 用户映射的 map_type_list 置空
        sync_rule["map_type_list"] = []
    elif sync_scope == "table":
        if not (source_schema and target_schema):
            raise ValueError("--sync-scope table 需要 --source-schema/--target-schema")
        sync_rule["table_map"] = [{
            "src_user": source_schema, "src_table": None,
            "dst_user": target_schema, "dst_table": None, "column": [],
        }]

    # i2Stream 在规则内部使用 Oracle 兼容路径处理 DM 目标端
    if target_db_type in ("dm", "dameng"):
        sync_rule["base_settings"]["db_set"][0]["tgt_db_type"] = "oracle"

    # 本控制台对 create_sync_rule 严格校验（未知字段报 1010001051），
    # 裁剪掉接口未声明的模板字段
    undeclared = ("map_type", "rule_uuids", "batch_basic_settings", "batch_full_sync_settings",
                  "batch_incre_sync_settings", "batch_advanced_settings",
                  "batch_full_sync_obj_filter", "batch_inc_sync_ddl_filter",
                  "batch_encrypt_compress", "rule_uuid", "prefix", "src_db_type",
                  "tgt_db_type", "src_db_type_uuid", "sync_mode", "kafka", "db_list",
                  "etl_settings", "syncMode")
    for key in undeclared:
        sync_rule.pop(key, None)

    return payload


def create_dm_rule(console: Console, *, rule_name: str, source_node: str, target_node: str,
                   sync_scope: str, source_schema: str | None = None,
                   target_schema: str | None = None,
                   enable_structure_migration: str | None = None,
                   enable_full_sync: str | None = None,
                   enable_incremental_sync: str | None = None,
                   start_rule_now: str | None = None,
                   trackmode: str | None = None,
                   template_path: str | None = None) -> str:
    """解析库节点名并创建 DM 同步规则，返回规则 uuid。"""
    src = console.find_db(source_node)
    if src is None:
        raise ConsoleMCPError(f"源端数据库节点不存在: {source_node}")
    tgt = console.find_db(target_node)
    if tgt is None:
        raise ConsoleMCPError(f"目标端数据库节点不存在: {target_node}")

    template = load_dm_template(template_path)
    payload = build_dm_sync_rule(
        template,
        rule_name=rule_name,
        sync_scope=sync_scope,
        source_db_uuid=src["db_uuid"],
        target_db_uuid=tgt["db_uuid"],
        source_db_type=src.get("db_type", "dm"),
        target_db_type=tgt.get("db_type"),
        source_db_type_uuid=src.get("type_uuid"),
        source_schema=source_schema,
        target_schema=target_schema,
        enable_structure_migration=enable_structure_migration,
        enable_full_sync=enable_full_sync,
        enable_incremental_sync=enable_incremental_sync,
        start_rule_now=start_rule_now,
        trackmode=trackmode,
    )
    return console.create_sync_rule(payload["sync_rule"])


RUNNING_STATUSES = (
    "STREAM_RULE_INCREMENT", "STREAM_RULE_CDC",
    "STREAM_RULE_FULL", "STREAM_RULE_FULLSYNC", "STREAM_RULE_SYNC",
)
STOPPED_STATUSES = ("STREAM_RULE_STOP", "STREAM_RULE_NONE")
TRANSITION_STATUSES = ("STREAM_RULE_UNKNOWN", "STREAM_RULE_MIGRATE", "STREAM_RULE_PREPARE")


def is_rule_state_ready(summary: dict, start_rule_now: bool = True) -> bool:
    """规则状态是否已稳定（离开 UNKNOWN；自动启动时也离开瞬时 STOP）。"""
    state = summary.get("rule_status") or ""
    if not state or "UNKNOWN" in state:
        return False
    if start_rule_now and state == "STREAM_RULE_STOP":
        return False
    return True


def wait_rule_ready(console: Console, rule_name: str, start_rule_now: bool = True,
                    timeout: int = 180, poll_interval: int = 5) -> dict:
    """等待规则状态稳定（创建后调用）。返回 {"state", "waited_seconds"}。"""
    start = time.time()
    while time.time() - start < timeout:
        summary = console.get_rule(rule_name)
        if summary and is_rule_state_ready(summary, start_rule_now):
            return {"state": summary["rule_status"],
                    "waited_seconds": int(time.time() - start)}
        time.sleep(poll_interval)
    return {"state": "TIMEOUT", "waited_seconds": timeout}
