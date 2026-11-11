"""任务依赖图：解析、校验与拓扑排序。

统一 executor / REST / MCP 三处的依赖解析，避免非法依赖被静默丢弃或
循环依赖被追加到执行序列末尾。所有校验错误都带具体的任务/循环路径。
"""
from __future__ import annotations

from collections import deque
from typing import Any, Iterable, Optional


class DependencyError(ValueError):
    """依赖图不合法（缺失、跨组、自依赖、环路或格式错误）。"""

    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("；".join(issues))


def parse_depends(depends: Any) -> list[int]:
    """把 task_list.depends（逗号分隔字符串 / 列表）解析为 int 列表。"""
    if depends is None or depends == "":
        return []
    if isinstance(depends, (list, tuple, set)):
        values = list(depends)
    else:
        values = [x.strip() for x in str(depends).split(",") if x.strip()]
    out: list[int] = []
    for value in values:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            raise DependencyError([f"依赖项格式错误: {value!r}（应为整数 tid）"])
    return out


def depends_to_str(depends: Optional[Iterable[int]]) -> Optional[str]:
    if not depends:
        return None
    return ",".join(str(int(x)) for x in depends)


def _tid(task: Any) -> int:
    return int(getattr(task, "tid"))


def _gid(task: Any) -> Optional[int]:
    value = getattr(task, "gid", None)
    return int(value) if value is not None else None


def _name(task: Any) -> str:
    return str(getattr(task, "name", "") or f"#{_tid(task)}")


def validate_tasks(tasks: Iterable[Any]) -> list[str]:
    """校验一组任务的依赖关系，返回问题列表（空列表表示合法）。"""
    task_list = list(tasks)
    by_tid = {_tid(t): t for t in task_list}
    issues: list[str] = []
    parsed: dict[int, list[int]] = {}

    for task in task_list:
        try:
            deps = parse_depends(getattr(task, "depends", None))
        except DependencyError as exc:
            issues.extend(f"任务 {_tid(task)}（{_name(task)}）: {msg}" for msg in exc.issues)
            parsed[_tid(task)] = []
            continue
        parsed[_tid(task)] = deps
        for dep_tid in deps:
            if dep_tid == _tid(task):
                issues.append(f"任务 {_tid(task)}（{_name(task)}）依赖自身")
                continue
            dep = by_tid.get(dep_tid)
            if dep is None:
                issues.append(
                    f"任务 {_tid(task)}（{_name(task)}）依赖的任务 {dep_tid} 不在本组"
                )
                continue
            if _gid(task) is not None and _gid(dep) is not None and _gid(task) != _gid(dep):
                issues.append(
                    f"任务 {_tid(task)}（{_name(task)}）依赖了其他组的任务 {dep_tid}"
                )

    issues.extend(_find_cycles(task_list, parsed))
    return issues


def _find_cycles(tasks: list[Any], parsed: dict[int, list[int]]) -> list[str]:
    """DFS 找环，返回类似 “10001 -> 10002 -> 10001” 的路径。"""
    by_tid = {_tid(t): t for t in tasks}
    state: dict[int, int] = {tid: 0 for tid in by_tid}  # 0=未访问 1=访问中 2=完成
    stack: list[int] = []
    found: list[str] = []
    seen_cycles: set[tuple[int, ...]] = set()

    def dfs(tid: int) -> None:
        state[tid] = 1
        stack.append(tid)
        for dep_tid in parsed.get(tid, []):
            if dep_tid not in by_tid:
                continue
            if state.get(dep_tid) == 0:
                dfs(dep_tid)
            elif state.get(dep_tid) == 1:
                # 栈中从 dep_tid 到当前 tid 构成环
                try:
                    start = stack.index(dep_tid)
                except ValueError:
                    start = 0
                cycle = tuple(stack[start:] + [dep_tid])
                canonical = tuple(sorted(cycle[:-1]))
                if canonical not in seen_cycles:
                    seen_cycles.add(canonical)
                    found.append("循环依赖: " + " -> ".join(str(x) for x in cycle))
        stack.pop()
        state[tid] = 2

    for tid in by_tid:
        if state[tid] == 0:
            dfs(tid)
    return found


def ensure_valid(tasks: Iterable[Any]) -> None:
    issues = validate_tasks(tasks)
    if issues:
        raise DependencyError(issues)


def topological_sort(tasks: Iterable[Any]) -> list[Any]:
    """按依赖拓扑排序；存在缺失/自依赖/环路时抛 DependencyError。"""
    task_list = list(tasks)
    ensure_valid(task_list)
    by_tid = {_tid(t): t for t in task_list}
    in_degree = {tid: 0 for tid in by_tid}
    graph: dict[int, list[int]] = {tid: [] for tid in by_tid}

    for task in task_list:
        for dep_tid in parse_depends(getattr(task, "depends", None)):
            if dep_tid in by_tid:
                in_degree[_tid(task)] += 1
                graph[dep_tid].append(_tid(task))

    # 用有序队列保证同层任务按 tid 稳定执行
    queue = deque(sorted(tid for tid, deg in in_degree.items() if deg == 0))
    sorted_tids: list[int] = []
    while queue:
        tid = queue.popleft()
        sorted_tids.append(tid)
        for nxt in sorted(graph[tid]):
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)

    if len(sorted_tids) != len(by_tid):
        # 正常情况下 ensure_valid 已拦住，这里作为防御
        remaining = [tid for tid in by_tid if tid not in sorted_tids]
        raise DependencyError([f"依赖图无法排序，可能存在循环: {remaining}"])
    return [by_tid[tid] for tid in sorted_tids]


def candidate_with_depends(task: Any, depends: Optional[Iterable[int]]) -> Any:
    """构造一个只替换 depends 的浅拷贝，供保存前校验。"""
    import copy

    clone = copy.copy(task)
    clone.depends = depends_to_str(depends)
    return clone
