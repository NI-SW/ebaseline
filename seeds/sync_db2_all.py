#!/usr/bin/env python3
"""同步 /home/stream-agent/test 下所有 DB2 相关测试用例到 ebaseline 统一任务组。

合并范围：
  - operate_db_node/db2/      (2 个用例：创建、修改)
  - operate_db_node/db2_delete/ (1 个用例：删除)
  - operate_db_node/db2_qa/     (7 个用例：QA 问答闭环)

用法：
    python seeds/sync_db2_all.py [--base-url http://127.0.0.1:8080] [--username admin] [--password admin123]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

STREAM_AGENT_TEST = Path("/home/stream-agent/test")
ENV_CONF_PATH = STREAM_AGENT_TEST / "env.conf"

# 用例目录映射：(目录名, 子类型, 任务名前缀)
CASE_DIRS = [
    ("operate_db_node/db2", "db2", ""),
    ("operate_db_node/db2_delete", "db2_delete", "删除"),
    ("operate_db_node/db2_qa", "db2_qa", "QA-"),
]


def load_env_conf() -> dict:
    """加载 env.conf，提取 DB2 + 节点相关变量。"""
    with open(ENV_CONF_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    keep_keys = {"NODE_NAME", "NODE_IP", "NODE_USER"}
    variables = {}
    for k, v in raw.items():
        if k.startswith("_comment"):
            continue
        if k in keep_keys or k.startswith("DB2_"):
            variables[k] = v
    return variables


def load_case(dir_path: Path, test_id: str) -> dict | None:
    """加载单个测试用例（input + expect）。"""
    input_file = dir_path / f"test{test_id}_input.json"
    # expect 目录结构：expect/operate_db_node/db2/test1_expect.json
    # dir_path = input/operate_db_node/db2，所以 expect 在 ../expect/operate_db_node/db2/
    expect_file = STREAM_AGENT_TEST / "expect" / dir_path.parent.name / dir_path.name / f"test{test_id}_expect.json"
    if not input_file.exists():
        return None
    with open(input_file, "r", encoding="utf-8") as f:
        input_data = json.load(f)
    expect_data = {}
    if expect_file.exists():
        with open(expect_file, "r", encoding="utf-8") as f:
            expect_data = json.load(f)
    return {"input": input_data, "expect": expect_data}


def build_task_payload(case: dict, prefix: str) -> dict:
    """将 stream-agent 测试用例转换为 ebaseline 任务格式。"""
    input_data = case["input"]
    expect_data = case["expect"]

    prompt = input_data.get("input", "")

    # 多轮对话（QA 闭环）
    conversation = []
    if "qa_missing" in input_data:
        for item in input_data["qa_missing"]:
            conversation.append({
                "field": item.get("field", ""),
                "answer": item.get("value", ""),
            })

    task_input = {"prompt": prompt}
    if conversation:
        task_input["conversation"] = conversation

    # expect 脚本：取第一条 check_command
    expect_script = None
    if expect_data.get("expected_results"):
        expect_script = expect_data["expected_results"][0].get("command")
        # 统一为工作空间相对路径（去掉 i2stream 子目录）
        if expect_script and "check_scripts/i2stream/" in expect_script:
            expect_script = expect_script.replace("check_scripts/i2stream/", "check_scripts/")

    # 前置脚本
    pre_script = None
    if input_data.get("prerequisite_commands"):
        pre_script = input_data["prerequisite_commands"][0].get("command")
        if pre_script and "check_scripts/i2stream/" in pre_script:
            pre_script = pre_script.replace("check_scripts/i2stream/", "check_scripts/")
        if pre_script and "setup_scripts/i2stream/" in pre_script:
            pre_script = pre_script.replace("setup_scripts/i2stream/", "setup_scripts/")

    # 清理脚本
    clean_script = input_data.get("cleanup_command")
    if clean_script and "check_scripts/i2stream/" in clean_script:
        clean_script = clean_script.replace("check_scripts/i2stream/", "check_scripts/")

    # 任务名称加前缀区分来源
    name = prefix + input_data.get("test_case", "未命名")

    return {
        "name": name[:32],
        "description": (input_data.get("description", "") or "")[:128],
        "depends": [],  # 依赖在导入后统一设置
        "pre_script": pre_script,
        "input": task_input,
        "expect": expect_script,
        "clean": clean_script,
    }


def main():
    parser = argparse.ArgumentParser(description="同步 DB2 全部测试用例到统一任务组")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin123")
    parser.add_argument("--group-name", default="db2_all_ops")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # 登录
    print(f"[1] 登录 {args.base_url} ...")
    resp = requests.post(
        f"{args.base_url}/api/v1/auth/login",
            json={"username": args.username, "password": args.password},
    )
    if resp.status_code != 200:
        print(f"登录失败: {resp.text}")
        sys.exit(1)
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    print("    登录成功")

    # 加载变量
    print("[2] 加载环境变量 ...")
    variables = load_env_conf()
    print(f"    共 {len(variables)} 个变量")

    # 创建任务组
    print(f"[3] 创建任务组 '{args.group_name}' ...")
    group_data = {
        "name": args.group_name,
        "description": "DB2 全部测试用例（创建/修改/删除/QA闭环）",
        "variables": variables,
        "python_version": "3.11",
    }
    if args.dry_run:
        print(f"    [dry-run] POST /api/v1/groups")
        gid = 1
    else:
        resp = requests.post(f"{args.base_url}/api/v1/groups", json=group_data, headers=headers)
        if resp.status_code not in (200, 201):
            print(f"创建任务组失败: {resp.text}")
            sys.exit(1)
        gid = resp.json()["id"]
        print(f"    任务组创建成功，gid={gid}")

    # 加载并创建任务
    print("[4] 导入测试用例 ...")
    tid_map: dict[str, int] = {}  # "dir-testid" -> ebaseline tid

    for dir_rel, sub_type, prefix in CASE_DIRS:
        dir_path = STREAM_AGENT_TEST / "input" / dir_rel
        if not dir_path.exists():
            print(f"    跳过 {dir_rel}（目录不存在）")
            continue

        for input_file in sorted(dir_path.glob("test*_input.json")):
            test_id = input_file.stem.replace("test", "").replace("_input", "")
            case = load_case(dir_path, test_id)
            if case is None:
                continue

            payload = build_task_payload(case, prefix)
            case_key = f"{sub_type}-{test_id}"
            print(f"    导入 {case_key}: {payload['name']}")

            if args.dry_run:
                print(f"      [dry-run] prompt 前80字符: {payload['input']['prompt'][:80]!r}")
                print(f"      [dry-run] expect: {payload['expect']!r}")
                tid_map[case_key] = hash(case_key) % 100000
            else:
                resp = requests.post(
                    f"{args.base_url}/api/v1/groups/{gid}/tasks",
                    json=payload, headers=headers,
                )
                if resp.status_code not in (200, 201):
                    print(f"      创建失败: {resp.text}")
                    continue
                tid = resp.json()["tid"]
                tid_map[case_key] = tid
                print(f"      tid={tid}")

    # 设置依赖关系
    print("[5] 设置任务依赖 ...")
    for dir_rel, sub_type, prefix in CASE_DIRS:
        dir_path = STREAM_AGENT_TEST / "input" / dir_rel
        if not dir_path.exists():
            continue
        for input_file in sorted(dir_path.glob("test*_input.json")):
            test_id = input_file.stem.replace("test", "").replace("_input", "")
            case_key = f"{sub_type}-{test_id}"
            if case_key not in tid_map:
                continue

            with open(input_file, "r", encoding="utf-8") as f:
                input_data = json.load(f)
            depends_on = input_data.get("depends_on", [])
            if not depends_on:
                continue

            # 解析依赖："operate_db_node.db2-1" -> "db2-1"
            dep_tids = []
            for dep_key in depends_on:
                parts = dep_key.rsplit(".", 1)
                if len(parts) == 2:
                    dep_sub = parts[1]  # "db2-1"
                    if dep_sub in tid_map:
                        dep_tids.append(tid_map[dep_sub])

            if dep_tids:
                tid = tid_map[case_key]
                print(f"    {case_key} (tid={tid}) 依赖 {dep_tids}")
                if not args.dry_run:
                    resp = requests.put(
                        f"{args.base_url}/api/v1/tasks/{tid}",
                        json={"depends": dep_tids},
                        headers=headers,
                    )
                    if resp.status_code != 200:
                        print(f"      设置依赖失败: {resp.text}")

    print(f"\n同步完成！任务组 gid={gid}，共 {len(tid_map)} 个任务")
    print("任务映射:")
    for k, v in sorted(tid_map.items()):
        print(f"  {k} -> tid={v}")


if __name__ == "__main__":
    main()
