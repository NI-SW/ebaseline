"""批次/任务执行指标计算。

口径参考 /share/test_sign.md：
- 五态：PASS / PARTIAL / FAIL / SKIP，另有 RUNNING / PENDING；
- strict 通过率 = PASS / (PASS + FAIL)，含 PARTIAL 通过率 = PASS / (PASS + PARTIAL + FAIL)；
- P95 升序取第 min(n-1, int(n*0.95)) 个，不做插值；
- token/耗时缺失时不按 0 计入均值，单独记录有效样本数。
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Iterable, Optional

VERDICT_TEXT = {
    "PASS": "通过",
    "PARTIAL": "部分通过",
    "FAIL": "失败",
    "SKIP": "跳过",
    "ERROR": "异常",
    "RUNNING": "执行中",
    "PENDING": "未执行",
}

# 非正常结束阶段：依赖不满足 / 前置脚本失败按 SKIP 处理
_STAGE_VERDICT = {
    "dependency": "SKIP",
    "pre_script": "SKIP",
    "parse_input": "FAIL",
    "exception": "FAIL",
}


def parse_metrics(raw: Any) -> dict:
    """把数据库中的 metrics 文本解析成 dict。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def result_verdict(result: int, result_detail: Optional[int], metrics: Optional[dict]) -> str:
    """把 result/result_detail/metrics.stage 归一化为五态。"""
    m = metrics or {}
    stage = m.get("stage")
    if result == 3:
        return "RUNNING"
    if result == 0:
        return "PENDING"
    if stage in _STAGE_VERDICT:
        return _STAGE_VERDICT[stage]
    if result == 1:
        if result_detail == 1:
            return "PARTIAL"
        return "PASS"
    if result == 2:
        if result_detail == 1:
            return "PARTIAL"
        return "FAIL"
    return "PENDING"


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def p95(values: Iterable[Optional[float]]) -> Optional[float]:
    """升序第 min(n-1, int(n*0.95)) 个（不插值）。"""
    vals = sorted(v for v in (_num(x) for x in values) if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(len(vals) * 0.95))]


def mean(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in (_num(x) for x in values) if v is not None]
    return sum(vals) / len(vals) if vals else None


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    return round(value, digits) if value is not None else None


def result_summary(result) -> dict:
    """单条 task_result 的指标摘要（供 API/UI 使用）。"""
    raw = parse_metrics(getattr(result, "metrics", None))
    verdict = result_verdict(
        getattr(result, "result", 0),
        getattr(result, "result_detail", None),
        raw,
    )
    tokens = raw.get("tokens")
    if isinstance(tokens, dict):
        total = _num(tokens.get("total"))
        if total is None:
            inp = _num(tokens.get("input")) or 0
            out = _num(tokens.get("output")) or 0
            total = inp + out if (inp or out) else None
        tokens = {
            "input": _num(tokens.get("input")),
            "output": _num(tokens.get("output")),
            "total": total,
        }
        if tokens["total"] is None:
            tokens = None
    else:
        tokens = None

    tool_names = raw.get("tool_names")
    return {
        "verdict": verdict,
        "verdict_text": VERDICT_TEXT.get(verdict, "未知"),
        "stage": raw.get("stage"),
        "duration_sec": _num(raw.get("duration_sec")),
        "agent_duration_sec": _num(raw.get("agent_duration_sec")),
        "rounds": _num(raw.get("rounds")),
        "tool_calls": _num(raw.get("tool_calls")),
        "tool_names": tool_names if isinstance(tool_names, list) else [],
        "tokens": tokens,
        "first_token_ms": _num(raw.get("first_token_ms")),
        "agent_success": raw.get("agent_success"),
        "expect_success": raw.get("expect_success"),
    }


def pending_summary() -> dict:
    """任务尚未执行时的指标摘要。"""
    return {
        "verdict": "PENDING",
        "verdict_text": VERDICT_TEXT["PENDING"],
        "stage": None,
        "duration_sec": None,
        "agent_duration_sec": None,
        "rounds": None,
        "tool_calls": None,
        "tool_names": [],
        "tokens": None,
        "first_token_ms": None,
        "agent_success": None,
        "expect_success": None,
    }


def _dedupe_by_tid(results: Iterable) -> list:
    """同一批次同一任务理论上只有一条，防御性取 id 最大的一条。"""
    by_tid: dict[int, Any] = {}
    for r in results:
        tid = getattr(r, "tid", None)
        if tid is None:
            continue
        prev = by_tid.get(tid)
        if prev is None or (getattr(r, "id", 0) or 0) >= (getattr(prev, "id", 0) or 0):
            by_tid[tid] = r
    return list(by_tid.values())


def compute_batch_metrics(results: Iterable, task_count: Optional[int] = None) -> dict:
    """汇总一个批次的结果指标。"""
    unique = _dedupe_by_tid(results)
    parsed = [result_summary(r) for r in unique]
    counts = Counter(p["verdict"] for p in parsed)

    total = task_count if task_count is not None else len(unique)
    running = counts.get("RUNNING", 0)
    pass_n = counts.get("PASS", 0)
    partial_n = counts.get("PARTIAL", 0)
    fail_n = counts.get("FAIL", 0)
    skip_n = counts.get("SKIP", 0)
    pending = counts.get("PENDING", 0) + max(0, total - len(unique))
    executed = pass_n + partial_n + fail_n

    durations = [p["duration_sec"] for p in parsed if p["duration_sec"] is not None]
    agent_durations = [p["agent_duration_sec"] for p in parsed if p["agent_duration_sec"] is not None]
    rounds = [p["rounds"] for p in parsed if p["rounds"] is not None]
    tool_calls = [p["tool_calls"] for p in parsed if p["tool_calls"] is not None]
    first_tokens = [p["first_token_ms"] for p in parsed if p["first_token_ms"] is not None]
    token_values = [
        p["tokens"]["total"] for p in parsed
        if p["tokens"] and p["tokens"].get("total") is not None
    ]

    return {
        "task_count": total,
        "result_count": len(unique),
        "pass": pass_n,
        "partial": partial_n,
        "fail": fail_n,
        "skip": skip_n,
        "running": running,
        "pending": pending,
        "executed": executed,
        "strict_pass_rate": _round(pass_n / (pass_n + fail_n), 4) if (pass_n + fail_n) else None,
        "pass_rate": _round(pass_n / executed, 4) if executed else None,
        "duration_avg": _round(mean(durations)),
        "duration_p95": _round(p95(durations)),
        "duration_samples": len(durations),
        "agent_duration_avg": _round(mean(agent_durations)),
        "agent_duration_samples": len(agent_durations),
        "rounds_avg": _round(mean(rounds)),
        "rounds_samples": len(rounds),
        "tool_calls_avg": _round(mean(tool_calls)),
        "tool_call_samples": len(tool_calls),
        "tokens_total": _round(sum(token_values), 0) if token_values else None,
        "tokens_avg": _round(mean(token_values)),
        "token_samples": len(token_values),
        "first_token_p95": _round(p95(first_tokens), 0),
        "first_token_samples": len(first_tokens),
        "verdict_counts": dict(counts),
    }
