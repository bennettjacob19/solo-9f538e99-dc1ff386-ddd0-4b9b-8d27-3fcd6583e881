"""交付前必测：在途包遇上改表。

包含两层：
1. 严格规划器绝不产出“砸在在途包当前交换机上”的改表动作；
2. what-if 非严格复放如实给出改道 / 黑洞后果（这也是交付前试跑的回归脚本）。
"""

from __future__ import annotations

from app.domain import advance, build_static, initial_state
from app.examples import (
    FEASIBLE_REPLACE_WAIT,
    INFLIGHT_DELETE_WHATIF,
    INFLIGHT_REPLACE_BLACKHOLE,
    INFLIGHT_WHATIF,
)
from app.planner import find_earliest_plan
from app.runner import Runner
from app.whatif import run_whatif


def _cap1_static():
    fx = FEASIBLE_REPLACE_WAIT
    return build_static(
        prefix=fx["prefix"], old_path=fx["old_path"], new_path=fx["new_path"],
        table_capacities=fx["table_capacities"],
        existing_positions=fx["existing_packets"], arrival_ticks=[],
        buffer_capacity=1, deadline=6,
    )


def test_strict_engine_blocks_replace_on_packet_switch():
    stc = _cap1_static()
    state = initial_state(stc)  # E01 初始就在 S
    # 第 1 拍立刻把 S 的旧下一跳 A 替换成新下一跳 B
    action = (("rep", "S", "A", "B"), 0, 1)  # 平面仍旧、入口开
    res = advance(stc, state, 1, action)
    assert not res.ok
    assert res.code == "in_flight_path_broken"
    assert "E01" in res.detail and "S" in res.detail


def test_safe_plan_moves_packet_off_s_before_replace():
    stc = _cap1_static()
    result = find_earliest_plan(stc)
    assert result.feasible
    runner = Runner(stc)
    runner.run(result.actions)

    replace_tick = None
    e01_left_s_tick = None
    for tr in runner.tick_reports:
        act = tr["table_action"]
        if act["kind"] == "replace" and act["switch"] == "S":
            replace_tick = tr["tick"]
        for hop in tr["hops"]:
            if hop["packet_id"] == "E01" and hop["at_switch"] == "S":
                # 这一拍从 S 跳走；表项操作发生在转发之前，
                # 所以安全方案里 S 的 replace 不能在“仍从 S 起跳”的这一拍或更早。
                e01_left_s_tick = tr["tick"]

    assert e01_left_s_tick == 1, "E01 初始在 S，第 1 拍应按旧项 S→A 离开"
    assert replace_tick is not None
    assert replace_tick >= 2, "替换 S 最早也得等 E01 第 1 拍离开 S 之后"

    e01 = next(p for p in runner.trajectories() if p["packet_id"] == "E01")
    assert e01["status"] == "delivered"


def test_whatif_replace_diverts_inflight_packet():
    out = run_whatif(
        INFLIGHT_WHATIF["drill"], INFLIGHT_WHATIF["script"],
        cutover_tick=INFLIGHT_WHATIF["cutover_tick"],
        closed_ticks=INFLIGHT_WHATIF["closed_ticks"],
        ticks=INFLIGHT_WHATIF["replay_ticks"],
    )
    codes = [v["code"] for v in out["violations"]]
    assert "in_flight_hit_changed_table" in codes
    v = next(v for v in out["violations"] if v["code"] == "in_flight_hit_changed_table")
    assert v["packet_id"] == "E01" and v["switch"] == "S"
    assert v["expected_nexthop"] == "A" and v["installed_nexthops"] == ["B"]

    traj = next(p for p in out["packet_trajectories"] if p["packet_id"] == "E01")
    assert "B" in traj["visited_switches"], "实装表应把包改道到 B"
    assert traj["final_state"] == "delivered_offpath"


def test_whatif_replace_without_completing_new_path_blackholes():
    out = run_whatif(
        INFLIGHT_REPLACE_BLACKHOLE["drill"], INFLIGHT_REPLACE_BLACKHOLE["script"],
        ticks=INFLIGHT_REPLACE_BLACKHOLE["replay_ticks"],
    )
    traj = next(p for p in out["packet_trajectories"] if p["packet_id"] == "E01")
    assert traj["visited_switches"][:2] == ["S", "B"]
    assert traj["final_state"] == "dropped"
    assert any(e["type"] == "drop" for e in traj["events"])


def test_whatif_delete_blackholes_packet():
    out = run_whatif(
        INFLIGHT_DELETE_WHATIF["drill"], INFLIGHT_DELETE_WHATIF["script"],
        ticks=INFLIGHT_DELETE_WHATIF["replay_ticks"],
    )
    codes = [v["code"] for v in out["violations"]]
    assert "in_flight_hit_changed_table" in codes
    traj = next(p for p in out["packet_trajectories"] if p["packet_id"] == "E01")
    assert traj["final_state"] == "dropped"
    assert any(e["type"] == "drop" for e in traj["events"])
