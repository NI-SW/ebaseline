"""Agent API 客户端：OpenAI Responses API 格式（/v1/responses，SSE 流式）。

复用 /home/stream-agent/test/test_i2stream.py 的调用逻辑：
- POST {base_url}/v1/responses 流式拉取 SSE
- 自动附加 AUTONOMY_DIRECTIVE（可在 config 关闭）
- 检测到 Agent 请求确认时，自动追问（最多 max_follow_ups 次）
- 支持 QA 闭环（conversation 字段）
"""
from __future__ import annotations

from typing import Optional

import json
import re
import threading
import time
from dataclasses import dataclass, field

import requests

from .config import get_config


AUTONOMY_DIRECTIVE = (
    "\n\n【系统指令】本次任务为无人值守自动测试，你已被预先授权执行所有必要操作，"
    "包括但不限于创建、修改、删除配置。不要反问用户，不要请求确认，"
    "遇到不确定的参数请根据上下文合理推断并继续执行。"
)

_FOLLOW_UP_INSTRUCTION = (
    "无需确认，请根据你已掌握的信息继续执行剩余操作，"
    "并在完成时给出明确的成功或失败结论。\n\n原始任务："
)

_MAX_QA_ROUNDS = 3


@dataclass
class AgentResult:
    """Agent 调用结果。"""
    success: bool
    output: str = ""
    error: str = ""
    returncode: int = -1
    duration_sec: float = 0.0
    rounds: int = 1
    tokens: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    qa_record: Optional[dict] = None


def _asks_for_confirmation(text: str) -> bool:
    """检测 Agent 是否在请求人工确认。"""
    if not text:
        return False
    patterns = [
        r"是否确认",
        r"请确认",
        r"确认一下",
        r"是否需要我",
        r"要我继续吗",
        r"请提供.*确认",
        r"待您确认",
        r"请回复确认",
    ]
    for p in patterns:
        if re.search(p, text):
            return True
    return False


def _asks_qa_question(text: str) -> bool:
    """检测 Agent 是否在索取缺失字段。"""
    if not text:
        return False
    patterns = [
        r"请提供",
        r"缺少.*信息",
        r"需要.*参数",
        r"没有.*字段",
        r"请补充",
        r"请告诉我",
    ]
    for p in patterns:
        if re.search(p, text):
            return True
    return False


def _detect_qa_fields(text: str, pending: list[dict]) -> list[str]:
    """从 Agent 提问中识别它要哪些字段。"""
    if not text:
        return []
    text_lower = text.lower()
    detected = []
    for item in pending:
        field_name = item["field"]
        synonyms = item.get("synonyms", [field_name])
        for syn in synonyms:
            if syn.lower() in text_lower:
                detected.append(field_name)
                break
    return detected


def _extract_final_text(payload: dict) -> str:
    """从 response.completed 的 payload 中提取最终文本。"""
    try:
        response = payload.get("response", {})
        output = response.get("output", [])
        for item in reversed(output):
            if item.get("type") == "message":
                content = item.get("content", [])
                for c in content:
                    if c.get("type") == "output_text":
                        return c.get("text", "")
    except Exception:
        pass
    return ""


def _accumulate_usage(total: dict, usage: dict) -> None:
    for k in ("input_tokens", "output_tokens", "total_tokens"):
        if k in usage:
            total[k] = total.get(k, 0) + usage[k]


def _run_agent_once(
    url: str,
    headers: dict,
    payload: dict,
    deadline: float,
) -> dict:
    """单轮 Agent 调用：POST /v1/responses，处理 SSE 流。"""
    result = {
        "text": "",
        "error": None,
        "final_payload": None,
        "usage": {},
    }
    text_parts: list[str] = []

    def _on_timeout():
        result["error"] = "Agent 调用超时"

    def _finalize():
        """确保 text 字段始终被设置。"""
        result["text"] = "".join(text_parts)
        return result

    timer = threading.Timer(max(1, deadline - time.time()), _on_timeout)
    timer.start()
    try:
        with requests.post(
            url,
            headers=headers,
            json=payload,
            stream=True,
            timeout=(10, max(10, deadline - time.time())),
        ) as resp:
            if resp.status_code != 200:
                result["error"] = f"HTTP {resp.status_code}: {resp.text[:500]}"
                return _finalize()
            for line in resp.iter_lines(decode_unicode=True):
                if result["error"]:
                    return _finalize()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type", "")

                if event_type == "response.output_text.delta":
                    delta = event.get("delta", "")
                    text_parts.append(delta)
                elif event_type == "response.completed":
                    result["final_payload"] = event
                    result["usage"] = event.get("response", {}).get("usage", {})
                    return _finalize()
                elif event_type == "response.error":
                    err = event.get("error", {})
                    result["error"] = f"Agent 错误: {err.get('message', str(err))}"
                    return _finalize()
    except requests.exceptions.Timeout:
        result["error"] = "Agent 调用超时"
    except Exception as e:
        result["error"] = f"Agent 调用异常: {e}"
    finally:
        timer.cancel()

    return _finalize()


def run_agent(
    base_url: str,
    api_key: str,
    input_text: str,
    timeout: Optional[int] = None,
    qa_missing: Optional[list[dict]] = None,
) -> AgentResult:
    """调用 Agent API 执行任务。

    Args:
        base_url: Agent 服务地址（如 http://192.168.34.65:58642）
        api_key: API key
        input_text: 自然语言指令
        timeout: 超时秒数（默认读 config）
        qa_missing: [{field, value, synonyms?}, ...]；非空时启用 QA 闭环

    Returns:
        AgentResult
    """
    cfg = get_config()
    timeout = timeout or cfg.agent_default_timeout
    max_follow_ups = cfg.agent_max_follow_ups
    autonomy = cfg.agent_autonomy_directive

    url = f"{base_url.rstrip('/')}/v1/responses"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    qa_mode = bool(qa_missing)
    final_input = input_text if (qa_mode or not autonomy) else input_text + AUTONOMY_DIRECTIVE

    payload = {
        "model": "hermes-agent",
        "input": final_input,
        "execute": True,
        "stream": True,
    }

    start_time = time.time()
    deadline = start_time + timeout
    text_parts: list[str] = []
    follow_ups = 0
    token_totals: dict = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    qa_fed: set[str] = set()
    qa_record: dict = {
        "requested_fields": [m["field"] for m in (qa_missing or [])],
        "asked_rounds": 0,
        "detected_fields": [],
        "fed_fields": [],
    } if qa_mode else {}

    while True:
        result = _run_agent_once(url, headers, payload, deadline)
        text_parts.append(result["text"])
        _accumulate_usage(token_totals, result.get("usage") or {})

        if result["error"]:
            if follow_ups > 0 and text_parts[0]:
                # 追加轮失败但首轮成功，保留已有结果
                break
            return AgentResult(
                success=False,
                output="".join(text_parts),
                error=result["error"],
                returncode=-1,
                duration_sec=round(time.time() - start_time, 1),
                rounds=follow_ups + 1,
                tokens=dict(token_totals),
                qa_record=qa_record if qa_mode else None,
            )

        final_payload = result["final_payload"]
        response_obj = final_payload.get("response") if final_payload else None
        status = response_obj.get("status") if isinstance(response_obj, dict) else None

        if status != "completed":
            return AgentResult(
                success=False,
                output="".join(text_parts),
                error=f"Agent 未正常完成 (status={status})",
                returncode=-1,
                duration_sec=round(time.time() - start_time, 1),
                rounds=follow_ups + 1,
                tokens=dict(token_totals),
                qa_record=qa_record if qa_mode else None,
            )

        round_text = _extract_final_text(final_payload) or result["text"]
        response_id = response_obj.get("id") if isinstance(response_obj, dict) else None

        # ── QA 闭环 ──
        if (qa_mode and response_id and follow_ups < _MAX_QA_ROUNDS
                and _asks_qa_question(round_text)):
            pending = [m for m in qa_missing if m["field"] not in qa_fed]
            if pending:
                detected = _detect_qa_fields(round_text, pending)
                to_feed = detected or [pending[0]["field"]]
                by_field = {m["field"]: m for m in qa_missing}
                lines = [f"{f}={by_field[f]['value']}" for f in to_feed if f in by_field]
                answer = (
                    "【补充信息】" + "；".join(lines)
                    + "。请据此直接完成操作，不要反问、不要请求确认，一次做完。"
                )
                for f in to_feed:
                    if f in by_field:
                        qa_fed.add(f)
                qa_record["asked_rounds"] += 1
                qa_record["detected_fields"].extend(to_feed)
                follow_ups += 1
                payload = {
                    "model": "hermes-agent",
                    "input": answer,
                    "execute": True,
                    "stream": True,
                    "previous_response_id": response_id,
                }
                continue

        # ── 自动追问确认 ──
        if follow_ups >= max_follow_ups or not _asks_for_confirmation(round_text) or not response_id:
            break

        follow_ups += 1
        payload = {
            "model": "hermes-agent",
            "input": _FOLLOW_UP_INSTRUCTION + input_text,
            "execute": True,
            "stream": True,
            "previous_response_id": response_id,
        }

    out = AgentResult(
        success=True,
        output="".join(text_parts),
        error="",
        returncode=0,
        duration_sec=round(time.time() - start_time, 1),
        rounds=follow_ups + 1,
        tokens=dict(token_totals),
    )
    if qa_mode:
        qa_record["fed_fields"] = sorted(qa_fed)
        qa_record["detected_fields"] = sorted(set(qa_record["detected_fields"]))
        out.qa_record = qa_record
    return out
