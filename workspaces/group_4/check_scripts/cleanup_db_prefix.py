#!/usr/bin/env python3
"""
按前缀删除数据库节点（测试清理兜底用）。

问答闭环负向用例在智能体"未索取就硬建"时可能残留 db2_qa_* 节点；
每个用例的 cleanup_command 调用本脚本，无论测试通过与否都清干净。

用法: python cleanup_db_prefix.py --prefix db2_qa [--dry-run] [--output-format json|text]

退出码: 0 — 清理成功（或本就无匹配节点）；1 — 删除过程出错
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

from console_mcp import Console, ConsoleMCPError  # noqa: E402


def cleanup(prefix: str, dry_run: bool = False) -> dict:
    console = Console()
    try:
        matched = [
            {"db_name": it.get("db_name"), "db_uuid": it.get("db_uuid")}
            for it in console.list_dbs(search_value=prefix)
            if str(it.get("db_name", "")).startswith(prefix) and it.get("db_uuid")
        ]
    except ConsoleMCPError as exc:
        return {"check_result": "FAIL", "prefix": prefix, "error": f"查询失败: {exc}"}

    if not matched:
        return {"check_result": "PASS", "prefix": prefix, "deleted": [],
                "note": "无匹配节点，无需清理"}

    if dry_run:
        return {"check_result": "PASS", "prefix": prefix,
                "matched": matched, "dry_run": True}

    try:
        deleted = console.delete_db_by_prefix(prefix, force=True)
    except Exception as exc:  # noqa: BLE001
        return {"check_result": "FAIL", "prefix": prefix,
                "matched": matched, "error": f"删除失败: {exc}"}

    return {"check_result": "PASS", "prefix": prefix,
            "deleted": deleted, "count": len(deleted)}


def main() -> int:
    parser = argparse.ArgumentParser(description="按前缀删除数据库节点（测试清理）")
    parser.add_argument("--prefix", required=True, help="节点名前缀，如 db2_qa")
    parser.add_argument("--dry-run", action="store_true", help="只列出匹配，不实际删除")
    parser.add_argument("--output-format", choices=["json", "text"], default="json")
    args = parser.parse_args()

    result = cleanup(args.prefix, dry_run=args.dry_run)

    if args.output_format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"结果: {result['check_result']} | 前缀: {result['prefix']}")
        if "deleted" in result:
            print(f"  已删除: {[d['db_name'] for d in result['deleted']]}")
        if "matched" in result:
            print(f"  匹配: {[m['db_name'] for m in result['matched']]}")
        if "error" in result:
            print(f"  错误: {result['error']}")

    return 0 if result["check_result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
