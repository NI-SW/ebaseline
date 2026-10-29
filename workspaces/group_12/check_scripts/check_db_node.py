#!/usr/bin/env python3
"""
检查数据库节点状态的脚本（走 i2up 控制台 MCP）

用法: python check_db_node.py --db-name <db_name> [--expected-status normal|deleted|any]
       [--expect-user U] [--expect-ip IP] [--expect-port PORT]

expected-status 说明:
  normal   — 节点存在且状态为 NORMAL
  deleted  — 节点不存在（用于删除验证；节点仍存在则 FAIL 并报告其当前状态）
  any      — 不断言状态（用于修改验证等；仅核对存在性与 --expect-* 字段）

--expect-user/--expect-ip/--expect-port:
  可选字段断言，与 expected-status 组合使用。任一不符则 FAIL 并列出全部差异。

退出码: 0 — 状态/字段符合预期；1 — 不存在/状态不符/字段不符/查询失败
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from console_mcp import Console  # noqa: E402


def check_db_node_status(db_name: str, expected_status: str = "normal",
                         expect_user: str | None = None,
                         expect_ip: str | None = None,
                         expect_port: str | None = None) -> dict:
    """检查数据库节点存在性、状态与关键字段。"""
    console = Console()
    item = console.find_db(db_name)

    expect_absent = expected_status.lower() in ("deleted", "not_exist", "absent")
    skip_status = expected_status.lower() in ("any", "skip")

    if item is None:
        if expect_absent:
            return {
                "db_node": db_name,
                "status": "NOT_FOUND",
                "check_result": "PASS",
                "expected_status": expected_status,
            }
        return {
            "db_node": db_name,
            "status": "NOT_FOUND",
            "check_result": "FAIL",
            "error": f"数据库节点 {db_name} 不存在",
        }

    if expect_absent:
        return {
            "db_node": db_name,
            "status": item.get("status") or "UNKNOWN",
            "db_type": item.get("db_type", "UNKNOWN"),
            "check_result": "FAIL",
            "expected_status": expected_status,
            "error": f"数据库节点 {db_name} 仍存在，未被删除",
        }

    # 节点存在：提取实际字段（与展示口径一致：db_list 优先，回退顶层配置）
    cfg = item.get("config") or {}
    db_list = cfg.get("db_list") or [{}]
    actual_user = str(cfg.get("username") or cfg.get("user") or "")
    actual_ip = str(db_list[0].get("ip") or cfg.get("db_ip") or "")
    actual_port = str(db_list[0].get("port") or cfg.get("port") or "")

    result = {
        "db_node": db_name,
        "db_type": item.get("db_type", "UNKNOWN"),
        "username": actual_user,
        "db_ip": actual_ip,
        "db_port": actual_port,
        "expected_status": expected_status,
    }

    field_errors = []
    if expect_user and actual_user != str(expect_user):
        field_errors.append(f"username={actual_user!r}，期望 {expect_user!r}")
    if expect_ip and actual_ip != str(expect_ip):
        field_errors.append(f"ip={actual_ip!r}，期望 {expect_ip!r}")
    if expect_port and actual_port != str(expect_port):
        field_errors.append(f"port={actual_port!r}，期望 {expect_port!r}")

    if skip_status:
        result["status"] = "SKIPPED"
        if field_errors:
            result["check_result"] = "FAIL"
            result["error"] = "字段不符: " + "; ".join(field_errors)
        else:
            result["check_result"] = "PASS"
        return result

    db_uuid = item["db_uuid"]
    statuses = console.db_status([db_uuid])
    entry = next((s for s in statuses if s.get("db_uuid") == db_uuid), None)

    if entry is None:
        result["status"] = "UNKNOWN"
        result["check_result"] = "FAIL"
        result["error"] = f"list_db_status 未返回 {db_name} 的状态"
        return result

    status = entry.get("status") or "UNKNOWN"
    result["status"] = status
    status_match = str(status).upper() == expected_status.upper()
    if status_match and field_errors:
        result["check_result"] = "FAIL"
        result["error"] = "状态符合但字段不符: " + "; ".join(field_errors)
    elif status_match:
        result["check_result"] = "PASS"
    else:
        result["check_result"] = "FAIL"
        if field_errors:
            result["error"] = "字段不符: " + "; ".join(field_errors)
    return result


def main():
    parser = argparse.ArgumentParser(description='检查数据库节点状态')
    parser.add_argument('--db-name', required=True, help='数据库节点名称')
    parser.add_argument('--expected-status', default='normal',
                        help='预期状态: normal（存在且正常）/ deleted（节点已删除）/ any（不断言状态）')
    parser.add_argument('--expect-user', default=None, help='断言节点用户名')
    parser.add_argument('--expect-ip', default=None, help='断言节点库IP')
    parser.add_argument('--expect-port', default=None, help='断言节点库端口')
    parser.add_argument('--output-format', choices=['json', 'text'], default='json', help='输出格式')

    args = parser.parse_args()

    try:
        result = check_db_node_status(args.db_name, args.expected_status,
                                      expect_user=args.expect_user,
                                      expect_ip=args.expect_ip,
                                      expect_port=args.expect_port)
    except Exception as exc:  # noqa: BLE001
        result = {"db_node": args.db_name, "status": "UNKNOWN", "check_result": "FAIL",
                  "error": f"{type(exc).__name__}: {exc}"}

    if args.output_format == 'json':
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")

    sys.exit(0 if result.get("check_result") == "PASS" else 1)


if __name__ == "__main__":
    main()
