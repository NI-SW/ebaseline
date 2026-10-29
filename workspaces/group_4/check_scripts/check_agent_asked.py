#!/usr/bin/env python3
"""
检查智能体是否在缺失字段场景下主动索取了信息、且框架已把正确值喂回（问答闭环）。

读取 run_agent 落盘的交互记录 JSON（qa_record），断言：
  --expect-field F   detected_fields 或 fed_fields 含字段 F（智能体确实索取了 F）
  --expect-fed       至少喂回过一个字段（闭环生效，而非智能体自行猜测）

交互记录来源：test_i2stream 主流程把 agent_result["qa_record"] 写入
output/<type>/testN_agent_qa.json，并经环境变量 I2STREAM_AGENT_QA_FILE 传递路径。

用法: python check_agent_asked.py --expect-field password [--expect-fed]
       [--qa-file PATH] [--output-format json|text]

退出码: 0 — 断言全部成立；1 — 记录缺失/字段未索取/未喂答
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _load_qa_record(qa_file: str | None) -> dict | None:
    path = qa_file or os.environ.get("I2STREAM_AGENT_QA_FILE", "")
    if not path:
        return None
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def check_agent_asked(expect_fields: list[str], expect_fed: bool,
                      qa_file: str | None) -> dict:
    record = _load_qa_record(qa_file)
    if record is None:
        return {
            "check_result": "FAIL",
            "error": "未找到问答交互记录（qa_record）——"
                     "智能体可能从未进入索取流程，或框架未落盘记录",
            "qa_file": qa_file or os.environ.get("I2STREAM_AGENT_QA_FILE", ""),
        }

    detected = record.get("detected_fields") or []
    fed = record.get("fed_fields") or []
    asked_rounds = record.get("asked_rounds", 0)

    field_status = {}
    all_ok = True
    for f in expect_fields:
        ok = (f in detected) or (f in fed)
        field_status[f] = {
            "detected": f in detected,
            "fed": f in fed,
            "ok": ok,
        }
        if not ok:
            all_ok = False

    fed_ok = (len(fed) > 0) if expect_fed else True
    if not fed_ok:
        all_ok = False

    return {
        "check_result": "PASS" if all_ok else "FAIL",
        "asked_rounds": asked_rounds,
        "detected_fields": detected,
        "fed_fields": fed,
        "field_status": field_status,
        "expect_fed": expect_fed,
        "fed_ok": fed_ok,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="检查智能体是否索取缺失字段且框架已喂答")
    parser.add_argument("--expect-field", action="append", default=[],
                        dest="expect_fields",
                        help="期望被索取的字段（可多次指定，如 --expect-field password）")
    parser.add_argument("--expect-fed", action="store_true",
                        help="断言至少喂回过一个字段（闭环生效）")
    parser.add_argument("--qa-file", default=None,
                        help="qa_record JSON 路径（默认读环境变量 I2STREAM_AGENT_QA_FILE）")
    parser.add_argument("--output-format", choices=["json", "text"], default="json")
    args = parser.parse_args()

    result = check_agent_asked(args.expect_fields, args.expect_fed, args.qa_file)

    if args.output_format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"结果: {result['check_result']}")
        for k in ("asked_rounds", "detected_fields", "fed_fields"):
            if k in result:
                print(f"  {k}: {result[k]}")
        if "error" in result:
            print(f"  错误: {result['error']}")

    return 0 if result["check_result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
