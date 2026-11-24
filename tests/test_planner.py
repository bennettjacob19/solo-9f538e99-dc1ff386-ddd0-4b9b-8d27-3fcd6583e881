"""规划器正确性：已知场景 + 随机配置的“规划—严格复放”一致性。"""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from app.domain import (
    ConfigError,
    advance,
    build_static,
    candidate_actions,
    initial_state,
)
from app.examples import (
    BUFFER_BOUNDARY_OK,
    BUFFER_ZERO_INFEASIBLE,
    DEADLINE_INFEASIBLE,
    FEASIBLE_DUAL,
    FEASIBLE_REPLACE_WAIT,
)
from app.planner import diagnose, find_earliest_plan, find_plan
from app.runner import Runner


def _stc(fixture: dict):
    return build_static(
        prefix=fixture.get("prefix", "p"),
        old_path=fixture["old_path"],
        new_path=fixture["new_path"],
        table_capacities=fixture["table_capacities"],
        existing_positions=fixture.get("existing_packets", []),
        arrival_ticks=fixture.get("arrival_ticks", []),
        buffer_capacity=fixture["buffer_capacity"],
        deadline=fixture["deadline"],
        release_per_tick=fixture.get("release_per_tick"),
    )


def _run_and_assert_goal(stc, actions):
    runner = Runner(stc)
    runner.run(actions)
    summ = runner.summary()
    assert summ["goal_reached"], "复放结束未达成目标"
    assert summ["all_delivered"]
    # 每一拍缓冲都没有溢出（严格执行本就保证，这里复核报告字段）
    for t in runner.tick_reports:
        assert t["buffer"]["used"] <= t["buffer"]["capacity"]
        assert t["buffer"]["overflow"] is False
    return runner


def test_feasible_dual_textbook_plan():
    stc = _stc(FEASIBLE_DUAL)
    result = find_earliest_plan(stc)
    assert result.feasible, "宽裕双版本场景必须有解"
    runner = _run_and_assert_goal(stc, result.actions)

    # 第 6 拍到的包在切换后必须沿新路送达
    a6 = next(p for p in runner.trajectories() if p["packet_id"] == "A6")
    assert a6["assigned_plane_at_release"] == "new"
    assert a6["status"] == "delivered"
    assert a6["committed_path"] == FEASIBLE_DUAL["new_path"]

    # 至少有一个包真正经新平面送达（“启用新路”不是只改标志）
    new_delivered = [
        p for p in runner.trajectories()
        if p["assigned_plane_at_release"] == "new" and p["status"] == "delivered"
    ]
    assert new_delivered

    # 终态没有旧路专用表项
    final_items = set(runner.state.items)
    for s, nh in stc.old_nh.items():
        exclusive = not (s in stc.new_nh and stc.new_nh[s] == nh)
        if exclusive:
            assert (s, nh) not in final_items


def test_cap1_replace_wait_is_safe():
    stc = _stc(FEASIBLE_REPLACE_WAIT)
    result = find_earliest_plan(stc)
    assert result.feasible
    runner = _run_and_assert_goal(stc, result.actions)
    e01 = next(p for p in runner.trajectories() if p["packet_id"] == "E01")
    assert e01["status"] == "delivered"
    # 存量包始终沿旧路，且每一跳都在旧路径上
    hops = [e for e in e01["events"] if e["type"] == "hop"]
    old_path = FEASIBLE_REPLACE_WAIT["old_path"]
    for h in hops:
        assert h["at_switch"] in old_path
        assert h["used_nexthop"] in old_path or h["used_nexthop"] == "DELIVER"


def test_buffer_zero_infeasible_but_one_feasible():
    stc0 = _stc(BUFFER_ZERO_INFEASIBLE)
    r0 = find_earliest_plan(stc0)
    assert not r0.feasible, "饱和共享分歧冲突下 B=0 应无解"
    diag = diagnose(stc0)
    assert "buffer_capacity" in diag["codes"]
    assert ["buffer"] in diag["relaxation_minimal_sets"]
    assert ["tables"] in diag["relaxation_minimal_sets"]

    stc1 = _stc(BUFFER_BOUNDARY_OK)
    r1 = find_earliest_plan(stc1)
    assert r1.feasible, "同一冲突 B=1 应能错峰解决"
    runner = _run_and_assert_goal(stc1, r1.actions)
    assert runner.buffer_peak <= 1


def test_deadline_infeasible_attribution():
    stc = _stc(DEADLINE_INFEASIBLE)
    r = find_earliest_plan(stc)
    assert not r.feasible
    diag = diagnose(stc)
    assert "deadline_propagation" in diag["codes"]
    assert diag["earliest_propagation_tick"] > DEADLINE_INFEASIBLE["deadline"]


def test_invalid_configs_collect_chinese_errors():
    with pytest.raises(ConfigError) as ei:
        build_static(
            prefix="p", old_path=["I", "A", "E"], new_path=["I", "B", "X", "Z"],
            table_capacities={"I": 1, "A": 1, "E": 1, "B": 1, "X": 1, "Z": 1},
            existing_positions=[], arrival_ticks=[], buffer_capacity=1, deadline=5,
        )
    assert any("同终点" in m for m in ei.value.errors)

    with pytest.raises(ConfigError) as ei2:
        build_static(
            prefix="p", old_path=["I", "A", "B", "C", "D", "E"],
            new_path=["I", "X", "Y", "Z", "W", "E"],
            table_capacities={}, existing_positions=[], arrival_ticks=[],
            buffer_capacity=1, deadline=5,
        )
    assert any("6" in m for m in ei2.value.errors)


def test_every_generated_feasible_plan_replays_to_goal():
    random.seed(1234)
    checked = 0
    for _ in range(250):
        n = random.randint(2, 6)
        sw = [f"S{i}" for i in range(n)]
        a, z = sw[0], sw[-1]
        middle = sw[1:-1]
        random.shuffle(middle)
        if len(middle) >= 2 and random.random() < 0.6:
            shared = [middle.pop(random.randrange(len(middle)))]
        else:
            shared = []
        cut = random.randint(0, len(middle))
        oldp = [a] + middle[:cut] + shared + [z]
        newp = [a] + middle[cut:] + shared + [z]
        if len(set(oldp)) != len(oldp) or len(set(newp)) != len(newp):
            continue
        union = set(oldp) | set(newp)
        try:
            stc = build_static(
                prefix="p", old_path=oldp, new_path=newp,
                table_capacities={s: random.choice([1, 1, 2, 3, 8]) for s in union},
                existing_positions=[
                    {"switch": random.choice(oldp[1:])}
                    for _ in range(random.randint(0, 3))
                ],
                arrival_ticks=random.sample(range(1, 13), random.randint(0, 8)),
                buffer_capacity=random.choice([0, 1, 2, 5, 9]),
                deadline=random.randint(4, 12),
            )
        except ConfigError:
            continue

        result = find_plan(stc, node_budget=300_000)
        if result.feasible:
            assert not result.cutoff
            runner = Runner(stc)
            runner.run(result.actions)
            assert runner.summary()["goal_reached"]
            checked += 1
    assert checked > 20, "随机用例中应当有相当数量的可行解被复放验证"


def test_candidate_actions_always_strictly_advance_or_reject():
    """任意候选动作推进时，要么产生合法新状态，要么给出明确拒绝码，绝不抛异常。"""
    stc = _stc(FEASIBLE_DUAL)
    state = initial_state(stc)
    for action in candidate_actions(stc, state):
        res = advance(stc, state, 1, action)
        assert (res.ok and res.state is not None) or (not res.ok and res.code)
