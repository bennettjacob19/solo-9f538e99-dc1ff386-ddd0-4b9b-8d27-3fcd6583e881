"""What-if 脚本复放（非严格）：允许值班员故意把改表动作砸在在途包头上，
观察“当拍实装表”真实会把包带到哪里（改道 / 黑洞丢弃 / 歧义 / 环路）。

正式方案接口走 app.domain.advance 的严格模式，永远不会产出这种动作；
本模块只服务 /api/v1/whatif 与交付前试跑，用来验证护栏和解释后果。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from .domain import DELIVER, MAX_TICKS, ConfigError, build_static
from .runner import table_view


@dataclass
class WhatIfAction:
    tick: int
    action: str            # add | delete | replace
    switch: str
    nexthop: str | None = None
    old_nexthop: str | None = None


def validate_script(
    stc: Any,
    actions: list[dict[str, Any]],
    cutover_tick: int | None,
    closed_ticks: list[int],
    ticks: int,
) -> list[str]:
    errs: list[str] = []
    by_tick: dict[int, int] = {}
    for i, a in enumerate(actions):
        t = a.get("tick")
        if not isinstance(t, int) or not (1 <= t <= MAX_TICKS):
            errs.append(f"第 {i + 1} 个脚本动作的 tick 必须在 1..{MAX_TICKS}")
            continue
        by_tick[t] = by_tick.get(t, 0) + 1
        if by_tick[t] > 1:
            errs.append(f"第 {t} 拍安排了超过一条表项动作（每拍至多增/删/替换一条）")
        if a.get("action") not in ("add", "delete", "replace"):
            errs.append(f"tick={t} 的 action 必须是 add/delete/replace")
        if a.get("switch") not in stc.caps:
            errs.append(f"tick={t} 的交换机 {a.get('switch')!r} 不在两条路径的交换机集合中")
    if cutover_tick is not None and not (1 <= cutover_tick <= MAX_TICKS):
        errs.append("cutover_tick 必须在 1..12 或为 null")
    for t in closed_ticks:
        if not (1 <= t <= MAX_TICKS):
            errs.append(f"closed_ticks 中的 {t} 超出 1..12")
    if not (1 <= ticks <= MAX_TICKS):
        errs.append("复放拍数 ticks 必须在 1..12")
    return errs


def run_whatif(
    config: dict[str, Any],
    script_actions: list[dict[str, Any]],
    *,
    cutover_tick: int | None = None,
    closed_ticks: list[int] | None = None,
    ticks: int | None = None,
) -> dict[str, Any]:
    stc = build_static(
        prefix=config.get("prefix", "10.0.0.0/24"),
        old_path=config["old_path"],
        new_path=config["new_path"],
        table_capacities=config["table_capacities"],
        existing_positions=config.get("existing_packets", []),
        arrival_ticks=config.get("arrival_ticks", []),
        buffer_capacity=config["buffer_capacity"],
        deadline=config.get("deadline", MAX_TICKS),
        release_per_tick=config.get("release_per_tick"),
    )
    closed = set(closed_ticks or [])
    horizon = ticks or stc.deadline

    errs = validate_script(stc, script_actions, cutover_tick, list(closed), horizon)
    if errs:
        raise ConfigError(errs)

    # 按拍索引脚本动作
    script_by_tick: dict[int, dict[str, Any]] = {a["tick"]: a for a in script_actions}

    items: set[tuple[str, str]] = {(s, stc.old_nh[s]) for s in stc.old_path}

    # 富包状态
    packets: dict[int, dict[str, Any]] = {}
    for spec in stc.packets:
        if spec.kind == "existing":
            idx = int(spec.init_pos)
            packets[spec.index] = {
                "id": spec.pid, "state": "in_network", "ver": 0,
                "route": list(stc.old_path), "idx": idx,
                "at": stc.old_path[idx], "off": False,
                "visited": [stc.old_path[idx]],
                "released_tick": 0, "delivered_tick": None,
                "events": [{"tick": 0, "type": "preinstalled_position",
                            "switch": stc.old_path[idx], "plane": "old"}],
            }
        else:
            packets[spec.index] = {
                "id": spec.pid, "state": "future", "ver": -1,
                "route": None, "idx": -1, "at": None, "off": False,
                "visited": [], "arrival": spec.arrival_tick,
                "released_tick": None, "delivered_tick": None,
                "events": [{"tick": int(spec.arrival_tick), "type": "scheduled_arrival"}],
            }

    buffer: list[int] = []
    violations: list[dict[str, Any]] = []
    tick_reports: list[dict[str, Any]] = []
    buffer_peak = 0

    def add_violation(t: int, code: str, zh: str, **extra: Any) -> None:
        v = {"tick": t, "code": code, "detail_zh": zh}
        v.update(extra)
        violations.append(v)

    for t in range(1, horizon + 1):
        # 1) 表项动作
        action_report: dict[str, Any] | None = None
        sa = script_by_tick.get(t)
        if sa:
            act, s = sa["action"], sa["switch"]
            if act == "add":
                nh = sa.get("nexthop") or stc.new_nh.get(s)
                action_report = {"kind": "add", "switch": s, "nexthop": nh}
                if nh is None:
                    add_violation(t, "bad_nexthop", f"{s} 无法推导 add 的下一跳，请显式给 nexthop")
                elif (s, nh) in items:
                    add_violation(t, "op_redundant", f"{s}→{nh} 已存在，add 被拒")
                elif sum(1 for ss, _ in items if ss == s) + 1 > stc.caps[s]:
                    add_violation(t, "install_rejected_table_full",
                                  f"{s} 表项已满（上限 {stc.caps[s]}），新增 {nh} 被交换机拒绝",
                                  switch=s)
                else:
                    items.add((s, nh))
            elif act == "delete":
                nh = sa.get("nexthop") or stc.old_nh.get(sa["switch"])
                action_report = {"kind": "delete", "switch": s, "nexthop": nh}
                if (s, nh) in items:
                    items.discard((s, nh))
                else:
                    add_violation(t, "entry_missing", f"{s}→{nh} 本就不在实装表，delete 空操作")
            else:  # replace
                old_nh = sa.get("old_nexthop") or stc.old_nh.get(s)
                new_nh = sa.get("nexthop") or stc.new_nh.get(s)
                action_report = {"kind": "replace", "switch": s,
                                "old_nexthop": old_nh, "nexthop": new_nh}
                if (s, old_nh) not in items:
                    add_violation(t, "entry_missing",
                                  f"第 {t} 拍替换 {s} 时，旧项 →{old_nh} 已不在",
                                  switch=s)
                elif (s, new_nh) in items:
                    add_violation(t, "op_redundant", f"{s}→{new_nh} 已存在，replace 无意义")
                else:
                    items.discard((s, old_nh))
                    items.add((s, new_nh))
                    if sum(1 for ss, _ in items if ss == s) > stc.caps[s]:
                        # 真实交换机上替换通常是原子的、条数不增；只有显式奇怪脚本会到这
                        add_violation(t, "table_full", f"{s} 替换后超表容")

        # 2) 入口平面 / 开关
        plane = 1 if (cutover_tick is not None and t >= cutover_tick) else 0
        opened = t not in closed
        cutover = cutover_tick == t

        # 3) 到包
        arrived_idx = stc.arrivals.get(t)
        arrived_ids: list[str] = []
        if arrived_idx is not None:
            p = packets[arrived_idx]
            p["state"] = "buffered"
            buffer.append(arrived_idx)
            arrived_ids.append(p["id"])
            p["events"].append({"tick": t, "type": "arrival", "detail_zh": "到达入口"})

        # 4) 放行（whatif 同样缺省一次全放）
        released: list[dict[str, str]] = []
        if opened and buffer:
            if stc.release_rate is None:
                take = list(buffer)
            else:
                take = buffer[: max(0, stc.release_rate)]
            buffer = buffer[len(take):]
            for idx in take:
                p = packets[idx]
                p["state"] = "in_network"
                p["ver"] = plane
                p["route"] = list(stc.paths[plane])
                p["idx"] = 0
                p["at"] = p["route"][0]
                p["off"] = False
                p["released_tick"] = t
                p["events"].append({
                    "tick": t, "type": "release", "plane": "old" if plane == 0 else "new",
                    "path": p["route"],
                    "detail_zh": "入口放行（whatif：路径按放行拍确定，但强改表不再受保护）"})
                released.append({"packet_id": p["id"], "plane": "old" if plane == 0 else "new"})

        overflow = len(buffer) > stc.buffer_cap
        if overflow:
            add_violation(t, "buffer_overflow",
                          f"入口缓冲 {len(buffer)} > 上限 {stc.buffer_cap}，发生溢出")
        buffer_peak = max(buffer_peak, len(buffer))

        # 5) 逐包走一跳（按当拍实装表，不保证仍在原路径上）
        hops_report: list[dict[str, Any]] = []
        for idx in sorted(packets):
            p = packets[idx]
            if p["state"] != "in_network":
                continue
            s = p["at"]
            at_items = sorted(nh for ss, nh in items if ss == s)

            expected_nh: str | None = None
            expected_to: str | None = None
            if not p["off"] and p["route"] is not None and 0 <= p["idx"] < len(p["route"]):
                if p["route"][p["idx"]] == s:
                    if p["idx"] + 1 < len(p["route"]):
                        expected_nh = p["route"][p["idx"] + 1]
                        expected_to = expected_nh
                    else:
                        expected_nh = DELIVER
                        expected_to = None

            on_track_item = expected_nh is not None and (s, expected_nh) in items

            if on_track_item:
                if expected_nh == DELIVER:
                    p["state"] = "delivered"
                    p["delivered_tick"] = t
                    p["events"].append({"tick": t, "type": "deliver", "at_switch": s})
                    hops_report.append({"packet_id": p["id"], "at_switch": s,
                                        "used_nexthop": DELIVER, "result": "delivered",
                                        "on_expected_path": True})
                else:
                    p["idx"] += 1
                    p["at"] = expected_to
                    p["visited"].append(expected_to)
                    p["events"].append({"tick": t, "type": "hop", "at_switch": s,
                                        "used_nexthop": expected_nh, "to_switch": expected_to,
                                        "on_expected_path": True})
                    hops_report.append({"packet_id": p["id"], "at_switch": s,
                                        "used_nexthop": expected_nh, "to_switch": expected_to,
                                        "result": "forwarded", "on_expected_path": True})
                continue

            # 预期表项不在 —— 在途包撞上改表/撤表
            if expected_nh is not None:
                add_violation(
                    t, "in_flight_hit_changed_table",
                    f"{p['id']} 是在{'旧' if p['ver'] == 0 else '新'}路放行的包，"
                    f"第 {t} 拍到 {s}：应走 {expected_nh}，但该表项已被增删改破坏；"
                    f"当拍实装下一跳={at_items or '无'}",
                    packet_id=p["id"], switch=s, expected_nexthop=expected_nh,
                    installed_nexthops=at_items,
                )
            else:
                add_violation(
                    t, "offpath_lookup",
                    f"{p['id']} 已偏离放行路径，第 {t} 拍在 {s} 查实装表：{at_items or '无表项'}",
                    packet_id=p["id"], switch=s, installed_nexthops=at_items,
                )

            if len(at_items) == 0:
                p["state"] = "dropped"
                p["events"].append({"tick": t, "type": "drop", "at_switch": s,
                                    "detail_zh": "无匹配表项，丢包/黑洞"})
                hops_report.append({"packet_id": p["id"], "at_switch": s,
                                    "result": "dropped", "on_expected_path": False})
            elif len(at_items) == 1:
                nh = at_items[0]
                hops_report.append({"packet_id": p["id"], "at_switch": s,
                                    "used_nexthop": nh, "result": "forwarded_actual",
                                    "on_expected_path": False})
                if nh == DELIVER:
                    p["state"] = "delivered_offpath"
                    p["delivered_tick"] = t
                    p["events"].append({"tick": t, "type": "deliver_offpath", "at_switch": s,
                                        "used_nexthop": nh,
                                        "detail_zh": "按实装表被送往目的地，但已偏离放行路径"})
                else:
                    if nh in p["visited"]:
                        p["state"] = "loop"
                        add_violation(t, "routing_loop",
                                      f"{p['id']} 实际被带回已访问过的 {nh}，形成环路",
                                      packet_id=p["id"])
                        p["events"].append({"tick": t, "type": "loop", "to_switch": nh})
                    else:
                        p["at"] = nh
                        p["off"] = True
                        p["idx"] = -1
                        p["visited"].append(nh)
                        p["events"].append({"tick": t, "type": "hop_actual", "at_switch": s,
                                            "used_nexthop": nh, "to_switch": nh,
                                            "detail_zh": f"按当拍实装表改道到 {nh}（偏离放行路径）"})
            else:
                p["state"] = "ambiguous"
                add_violation(t, "ambiguous_forwarding",
                              f"{s} 对该包存在多个可能下一跳 {at_items}，演练将其标记为歧义并暂停",
                              packet_id=p["id"])

        tick_reports.append({
            "tick": t,
            "table_action": action_report or {"kind": "noop"},
            "ingress": {"plane": "new" if plane else "old", "open": opened,
                        "cutover_this_tick": cutover},
            "arrivals": arrived_ids,
            "released": released,
            "tables": table_view(stc, SimpleNamespace(items=frozenset(items))),
            "buffer": {"used": len(buffer), "capacity": stc.buffer_cap,
                       "packet_ids": [packets[i]["id"] for i in buffer],
                       "overflow": overflow},
            "hops": hops_report,
            "packet_states": [
                {"packet_id": packets[i]["id"], "state": packets[i]["state"],
                 "switch": packets[i]["at"]}
                for i in sorted(packets)
            ],
        })

    trajectories = []
    n_delivered = n_off = n_bad = 0
    for idx in sorted(packets):
        p = packets[idx]
        if p["state"] == "delivered":
            n_delivered += 1
        elif p["state"] == "delivered_offpath":
            n_off += 1
        elif p["state"] in ("dropped", "loop", "ambiguous"):
            n_bad += 1
        trajectories.append({
            "packet_id": p["id"],
            "final_state": p["state"],
            "released_tick": p["released_tick"],
            "delivered_tick": p["delivered_tick"],
            "visited_switches": p["visited"],
            "events": p["events"],
        })

    return {
        "mode": "whatif_unsafe_replay",
        "summary_zh": (
            f"What-if 复放 {horizon} 拍：沿放行路径正常送达 {n_delivered}，"
            f"被改道后偏路径送达 {n_off}，丢包/环路/歧义 {n_bad}，"
            f"违规/异常事件 {len(violations)} 条。"
            "该接口不产出可执行值班方案，只演示强改表对在途包的真实后果。"
        ),
        "ticks": tick_reports,
        "violations": violations,
        "packet_trajectories": trajectories,
        "buffer_peak_used": buffer_peak,
    }
