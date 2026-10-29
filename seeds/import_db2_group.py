#!/usr/bin/env python3
"""导入 DB2 测试用例组到 ebaseline 系统。

数据来源：/home/stream-agent/test/input/operate_db_node/db2/
用法：
    python seeds/import_db2_group.py [--env-conf /path/to/env.conf]

前提：
    - ebaseline 服务已启动
    - 已存在 admin 用户（默认 admin/admin123）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 从现有测试框架读取用例
STREAM_AGENT_TEST = Path("/home/stream-agent/test")
DB2_INPUT_DIR = STREAM_AGENT_TEST / "input" / "operate_db_node" / "db2"
DB2_EXPECT_DIR = STREAM_AGENT_TEST / "expect" / "operate_db_node" / "db2"
ENV_CONF_PATH = STREAM_AGENT_TEST / "env.conf"


def load_env_conf() -> dict:
    """加载 stream-agent 的 env.conf，提取 DB2 相关变量。"""
    with open(ENV_CONF_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    # 只保留 DB2 相关 + 节点信息
    keep_keys = {"NODE_NAME", "NODE_IP", "NODE_USER"}
    variables = {}
    for k, v in raw.items():
        if k.startswith("_comment"):
            continue
        if k in keep_keys or k.startswith("DB2_"):
            variables[k] = v
    return variables


def load_test_cases() -> list[dict]:
    """加载 DB2 测试用例（input + expect）。"""
    cases = []
    for input_file in sorted(DB2_INPUT_DIR.glob("test*_input.json")):
        test_id = input_file.stem.replace("test", "").replace("_input", "")
        expect_file = DB2_EXPECT_DIR / f"test{test_id}_expect.json"
        with open(input_file, "r", encoding="utf-8") as f:
            input_data = json.load(f)
        expect_data = {}
        if expect_file.exists():
            with open(expect_file, "r", encoding="utf-8") as f:
                expect_data = json.load(f)
        cases.append({
            "test_id": test_id,
            "input": input_data,
            "expect": expect_data,
        })
    return cases


def build_task_payload(case: dict, gid: int) -> dict:
    """将 stream-agent 测试用例转换为 ebaseline 任务格式。"""
    input_data = case["input"]
    expect_data = case["expect"]

    # 构造 prompt（保留 {VAR} 占位符，由执行时渲染）
    prompt = input_data.get("input", "")

    # 构造 conversation（多轮对话）
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

    # 构造 expect 脚本命令
    # 现有框架中 expect 是多个 check_commands，ebaseline 目前只支持单条 expect 脚本
    # 策略：取第一条 check_command 作为 expect，其余忽略（或后续扩展支持多条）
    expect_script = None
    if expect_data.get("expected_results"):
        first_check = expect_data["expected_results"][0]
        expect_script = first_check.get("command")

    # 构造 pre_script（prerequisite_commands 中的第一条）
    pre_script = None
    if input_data.get("prerequisite_commands"):
        pre_script = input_data["prerequisite_commands"][0].get("command")

    # 构造 clean（cleanup_command）
    clean_script = input_data.get("cleanup_command")

    # 构造 depends
    depends = []
    if input_data.get("depends_on"):
        # stream-agent 的 depends_on 格式如 ["operate_db_node.db2-1"]
        # ebaseline 的 depends 是组内 tid 列表
        # 这里先留空，由导入脚本在创建任务后手动设置
        pass

    return {
        "name": input_data.get("test_case", f"db2-test{case['test_id']}")[:32],
        "description": input_data.get("description", "")[:128],
        "depends": depends,
        "pre_script": pre_script,
        "input": task_input,
        "expect": expect_script,
        "clean": clean_script,
    }


def main():
    parser = argparse.ArgumentParser(description="导入 DB2 测试用例组")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080", help="ebaseline REST API 地址")
    parser.add_argument("--username", default="admin", help="登录用户名")
    parser.add_argument("--password", default="admin123", help="登录密码")
    parser.add_argument("--group-name", default="db2_node_ops", help="任务组名称")
    parser.add_argument("--dry-run", action="store_true", help="仅打印不实际创建")
    args = parser.parse_args()

    # 1. 登录
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
    print(f"    登录成功")

    # 2. 加载变量
    print(f"[2] 加载环境变量 ...")
    variables = load_env_conf()
    print(f"    共 {len(variables)} 个变量")

    # 3. 创建任务组
    print(f"[3] 创建任务组 '{args.group_name}' ...")
    group_data = {
        "name": args.group_name,
        "description": "DB2 数据库节点操作测试（创建、修改）",
        "variables": variables,
    }
    if args.dry_run:
        print(f"    [dry-run] POST /api/v1/groups")
        print(f"    {json.dumps(group_data, ensure_ascii=False, indent=2)[:500]}")
        gid = 1
    else:
        resp = requests.post(
            f"{args.base_url}/api/v1/groups",
            json=group_data,
            headers=headers,
        )
        if resp.status_code not in (200, 201):
            print(f"创建任务组失败: {resp.text}")
            sys.exit(1)
        gid = resp.json()["id"]
        print(f"    任务组创建成功，gid={gid}")

    # 4. 加载并创建任务
    print(f"[4] 导入测试用例 ...")
    cases = load_test_cases()
    tid_map: dict[str, int] = {}  # stream-agent test_id -> ebaseline tid

    for case in cases:
        test_id = case["test_id"]
        payload = build_task_payload(case, gid)
        print(f"    导入 test{test_id}: {payload['name']}")

        if args.dry_run:
            print(f"      [dry-run] POST /api/v1/groups/{gid}/tasks")
            print(f"      {json.dumps(payload, ensure_ascii=False, indent=2)[:500]}")
            tid_map[test_id] = int(test_id)
        else:
            resp = requests.post(
                f"{args.base_url}/api/v1/groups/{gid}/tasks",
                json=payload,
                headers=headers,
            )
            if resp.status_code not in (200, 201):
                print(f"      创建任务失败: {resp.text}")
                continue
            tid = resp.json()["tid"]
            tid_map[test_id] = tid
            print(f"      任务创建成功，tid={tid}")

    # 5. 设置依赖关系
    print(f"[5] 设置任务依赖 ...")
    for case in cases:
        test_id = case["test_id"]
        input_data = case["input"]
        depends_on = input_data.get("depends_on", [])
        if not depends_on:
            continue

        # 解析依赖：stream-agent 格式 "operate_db_node.db2-1" -> 取最后的数字
        dep_tids = []
        for dep_key in depends_on:
            parts = dep_key.rsplit("-", 1)
            if len(parts) == 2:
                dep_test_id = parts[1]
                if dep_test_id in tid_map:
                    dep_tids.append(tid_map[dep_test_id])

        if not dep_tids:
            continue

        tid = tid_map[test_id]
        print(f"    任务 {tid} 依赖 {dep_tids}")
        if not args.dry_run:
            resp = requests.put(
                f"{args.base_url}/api/v1/tasks/{tid}",
                json={"depends": dep_tids},
                headers=headers,
            )
            if resp.status_code != 200:
                print(f"      设置依赖失败: {resp.text}")

    print(f"\n导入完成！任务组 gid={gid}，共 {len(tid_map)} 个任务")
    print(f"任务 ID 映射: {tid_map}")


if __name__ == "__main__":
    main()
