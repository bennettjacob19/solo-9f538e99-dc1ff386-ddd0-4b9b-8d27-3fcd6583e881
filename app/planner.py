"""在 ≤12 拍的小状态空间内做完备 DFS 搜索，并在无解时做约束归因。"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any

from .domain import (
    MAX_TICKS,
    Action,
    State,
    advance,
    candidate_actions,
    earliest_delivery_bound_ok,
    goal_reached,
    initial_state,
    min_ops_remaining,
)
from .domain import Static


@dataclass
class FindResult:
    feasible: bool
    actions: tuple[Action, ...] | None
    nodes: int
    elapsed_ms: float
    cutoff: bool = False


def find_plan(stc: Static, *, node_budget: int = 1_000_000) -> FindResult:
    """返回任意一个合法方案。深度受 deadline（≤12）限制，状态做记忆化失败剪枝。"""
    t0 = time.perf_counter()
    start = initial_state(stc)
    if not earliest_delivery_bound_ok(stc, start, 0):
        return FindResult(False, None, 0, 0.0)

    failed: set[tuple[int, State]] = set()
    nodes = 0
    cutoff = False

    def dfs(tick: int, state: State) -> tuple[Action, ...] | None:
        nonlocal nodes, cutoff
        if goal_reached(stc, state):
            return ()
        if tick > stc.deadline:
            return None
        key = (tick, state)
        if key in failed:
            return None

        nodes += 1
        if nodes > node_budget:
            cutoff = True
            return None

        # 此刻到 deadline 还剩 deadline-tick+1 次操作机会
        if min_ops_remaining(stc, state) > stc.deadline - tick + 1:
            failed.add(key)
            return None

        for action in candidate_actions(stc, state):
            res = advance(stc, state, tick, action)
            if not res.ok or res.state is None:
                continue
            nxt = res.state
            if not earliest_delivery_bound_ok(stc, nxt, tick):
                continue
            if min_ops_remaining(stc, nxt) > stc.deadline - tick:
                continue
            suffix = dfs(tick + 1, nxt)
            if suffix is not None:
                return (action,) + suffix

        failed.add(key)
        return None

    actions = dfs(1, start)
    elapsed = (time.perf_counter() - t0) * 1000
    if cutoff and actions is None:
        return FindResult(False, None, nodes, elapsed, cutoff=True)
    return FindResult(actions is not None, actions, nodes, elapsed)


def find_earliest_plan(stc: Static, *, node_budget: int = 1_000_000) -> FindResult:
    """在 1..deadline 拍中逐层尝试，返回完成拍最早的合法方案。

    浅层搜索很快；这样 /docs 里看到的是“尽快铺路/切换/撤旧”的方案，
    而不是拖到最后一拍的投机动作序列。
    """
    last: FindResult | None = None
    total_nodes = 0
    t0 = time.perf_counter()
    for horizon in range(1, stc.deadline + 1):
        sub = replace(stc, deadline=horizon)
        r = find_plan(sub, node_budget=node_budget)
        total_nodes += r.nodes
        last = r
        if r.feasible:
            return FindResult(
                True, r.actions, total_nodes,
                (time.perf_counter() - t0) * 1000, cutoff=r.cutoff,
            )
    assert last is not None
    return FindResult(
        False, None, total_nodes,
        (time.perf_counter() - t0) * 1000, cutoff=last.cutoff,
    )


def minimum_table_ops(stc: Static) -> int:
    """静态最少表项操作数（每台差异机器 0/1，replace 可同时立新撤旧）。"""
    initial = frozenset((s, stc.old_nh[s]) for s in stc.old_path)
    n = 0
    for s in stc.switches:
        need_new = s in stc.new_nh and (s, stc.new_nh[s]) not in initial
        old_exclusive = (
            s in stc.old_nh
            and not (s in stc.new_nh and stc.new_nh[s] == stc.old_nh[s])
        )
        old_present = old_exclusive and (s, stc.old_nh[s]) in initial
        if need_new or old_present:
            n += 1
    # 同机“新项缺失 + 旧专用项在”可由一次 replace 同时完成，故按差异机器数计数。
    return n


def _relaxed(stc: Static, *, horizon12: bool, inf_buffer: bool, big_tables: bool,
             extended_horizon: int | None = None) -> Static:
    changes: dict[str, Any] = {}
    if extended_horizon is not None:
        changes["deadline"] = extended_horizon
    elif horizon12 and stc.deadline < MAX_TICKS:
        changes["deadline"] = MAX_TICKS
    if inf_buffer:
        changes["buffer_cap"] = 10**6
    if big_tables:
        changes["caps"] = {s: 64 for s in stc.caps}
    return replace(stc, **changes) if changes else stc


def diagnose(stc: Static, *, node_budget: int = 400_000) -> dict[str, Any]:
    """对无解配置做约束归因：分别放松 期限 / 缓冲 / 表容，看重放松后是否可解。"""
    from .domain import static_deadline_lower_bound

    earliest_worst, timing_notes = static_deadline_lower_bound(stc)
    min_ops = minimum_table_ops(stc)

    static_hits: list[dict[str, Any]] = []
    if earliest_worst > stc.deadline:
        static_hits.append({
            "code": "deadline_propagation",
            "label_zh": "完成期限（传播时间）",
            "detail_zh": (
                f"纯传播至少要到第 {earliest_worst} 拍才可能送完，"
                f"早于期限第 {stc.deadline} 拍；即使表项/缓冲都不限也来不及。"
            ),
        })
    if min_ops > stc.deadline:
        static_hits.append({
            "code": "ops_rate",
            "label_zh": "每拍至多一条表项操作",
            "detail_zh": (
                f"新旧路径在 {min_ops} 台交换机上存在表态差异，每台至少需要一次"
                f"增/删/替换，共至少 {min_ops} 次操作，但期限内只有 {stc.deadline} 拍。"
            ),
        })

    dims = ("deadline", "buffer", "tables")
    labels = {
        "deadline": f"把完成期限从第 {stc.deadline} 拍放宽到演练上限第 {MAX_TICKS} 拍",
        "buffer": f"把入口缓冲上限从 {stc.buffer_cap} 放宽到不受限",
        "tables": "放宽各交换机表项上限",
    }

    def feasible_with(mask: tuple[str, ...]) -> bool:
        r = _relaxed(
            stc,
            horizon12="deadline" in mask,
            inf_buffer="buffer" in mask,
            big_tables="tables" in mask,
        )
        return find_plan(r, node_budget=node_budget).feasible

    feasible_masks: list[tuple[str, ...]] = []
    cutoff_masks: list[tuple[str, ...]] = []

    # 先单约束放松，再两两，最后全部，寻找“最小可行放松集”。
    for r in (1, 2, 3):
        from itertools import combinations
        for mask in combinations(dims, r):
            outcome = find_plan(_relaxed(
                stc,
                horizon12="deadline" in mask,
                inf_buffer="buffer" in mask,
                big_tables="tables" in mask,
            ), node_budget=node_budget)
            if outcome.feasible:
                feasible_masks.append(mask)
            elif outcome.cutoff:
                cutoff_masks.append(mask)

    minimal_sets: list[tuple[str, ...]] = []
    for mask in feasible_masks:
        if not any(set(smaller) < set(mask) for smaller in feasible_masks):
            minimal_sets.append(mask)

    # 结构性兜底：全部放宽并给足 24 拍，正常输入必然可解。
    structural = find_plan(
        _relaxed(stc, horizon12=False, inf_buffer=True, big_tables=True, extended_horizon=24),
        node_budget=node_budget,
    )

    explanations: list[str] = []
    codes: list[str] = [h["code"] for h in static_hits]
    for h in static_hits:
        explanations.append(h["detail_zh"])

    if minimal_sets:
        for mask in minimal_sets:
            if len(mask) == 1:
                d = mask[0]
                code = {
                    "deadline": "deadline_tight",
                    "buffer": "buffer_capacity",
                    "tables": "table_capacity",
                }[d]
                dim_name = {
                    "deadline": "完成期限",
                    "buffer": "入口缓冲上限",
                    "tables": "交换机表项上限",
                }[d]
                codes.append(code)
                explanations.append(
                    f"搜索验证：仅{labels[d]}，方案即成立——主要受【{dim_name}】约束。"
                )
            else:
                names = "、".join({
                    "deadline": "完成期限", "buffer": "入口缓冲上限", "tables": "表项上限"
                }[d] for d in mask)
                codes.append("combined_" + "_".join(mask))
                explanations.append(
                    "搜索验证：必须同时放宽 " + " 与 ".join(labels[d] for d in mask)
                    + f" 才有解，即受【{names}】的组合约束。"
                )
    elif structural.feasible:
        explanations.append(
            "在 12 拍上限内放松单项/组合约束仍未找到方案，但放宽到 24 拍且不限缓冲/表容后可解，"
            "说明主要受演练拍数（完成期限 + 每拍一次操作/逐拍传播）的综合时序约束。"
        )
        codes.append("horizon_timing")
    else:
        explanations.append("即使大幅放宽期限、缓冲与表容仍不可解，可能是输入模型本身的问题。")
        codes.append("structural")

    if cutoff_masks:
        explanations.append("（部分归因搜索触及节点预算，结论以已完成的放松实验为准。）")

    # 去重保序
    seen: set[str] = set()
    uniq_codes = [c for c in codes if not (c in seen or seen.add(c))]

    return {
        "codes": uniq_codes,
        "reasons_zh": explanations,
        "earliest_propagation_tick": earliest_worst,
        "timing_notes": timing_notes,
        "minimum_table_ops": min_ops,
        "deadline": stc.deadline,
        "relaxation_minimal_sets": [list(m) for m in minimal_sets],
        "structural_extended_feasible": structural.feasible,
    }
