"""把规划出的动作交给同一个严格状态机逐拍执行，并生成 JSON 友好的逐拍报告。"""

from __future__ import annotations

from typing import Any

from .domain import (
    DELIVER,
    ST_BUFFERED,
    ST_DELIVERED,
    ST_FUTURE,
    ST_NET,
    Action,
    Static,
    advance,
    goal_reached,
    initial_state,
)

PLANE_ZH = {0: "old", 1: "new"}


def describe_op(op: tuple[Any, ...], stc: Static | None = None) -> dict[str, Any]:
    kind = op[0]
    if kind == "noop":
        return {"kind": "noop", "switch": None, "nexthop": None,
                "label_zh": "本拍不改表（noop）"}
    if kind == "add":
        _, s, nh = op
        ver = "new" if stc and stc.new_nh.get(s) == nh else None
        return {"kind": "add", "switch": s, "nexthop": nh, "version": ver,
                "label_zh": f"在 {s} 增表项：前缀 → {nh}"
                            + ("（新路下一跳）" if ver == "new" else "")}
    if kind == "del":
        _, s, nh = op
        return {"kind": "delete", "switch": s, "nexthop": nh, "version": "old",
                "label_zh": f"删除 {s} 的旧路专用表项：前缀 → {nh}"}
    if kind == "rep":
        _, s, old_nh, new_nh = op
        return {"kind": "replace", "switch": s, "old_nexthop": old_nh,
                "nexthop": new_nh, "version": "old_to_new",
                "label_zh": f"替换 {s} 表项下一跳：{old_nh} → {new_nh}（一条操作）"}
    return {"kind": str(kind), "label_zh": str(op)}


def table_view(stc: Static, state: Any) -> list[dict[str, Any]]:
    by_switch: dict[str, list[str]] = {s: [] for s in stc.switches}
    for s, nh in state.items:
        by_switch.setdefault(s, []).append(nh)

    view: list[dict[str, Any]] = []
    for s in stc.switches:
        nhs = sorted(by_switch.get(s, []))
        entries = []
        for nh in nhs:
            serves: list[str] = []
            if stc.old_nh.get(s) == nh:
                serves.append("old")
            if stc.new_nh.get(s) == nh:
                serves.append("new")
            entries.append({
                "nexthop": nh,
                "nexthop_zh": "送往目的地（出口）" if nh == DELIVER else nh,
                "serves_versions": serves,
            })
        used = len(nhs)
        cap = stc.caps[s]
        view.append({
            "switch": s,
            "used": used,
            "capacity": cap,
            "remaining": cap - used,
            "full": used >= cap,
            "usage_zh": f"{used}/{cap}",
            "entries": entries,
        })
    return view


def packet_positions_view(stc: Static, state: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec, pk in zip(stc.packets, state.pks):
        if pk.st == ST_FUTURE:
            item = {"packet_id": spec.pid, "state": "not_arrived",
                    "state_zh": "未到达", "plane": None, "switch": None}
        elif pk.st == ST_BUFFERED:
            item = {"packet_id": spec.pid, "state": "buffered",
                    "state_zh": "入口缓冲中", "plane": None, "switch": None}
        elif pk.st == ST_NET:
            sw = stc.paths[pk.ver][pk.pos]
            item = {"packet_id": spec.pid, "state": "in_network",
                    "state_zh": f"在{ '旧' if pk.ver == 0 else '新'}路途中",
                    "plane": PLANE_ZH[pk.ver], "switch": sw}
        else:
            item = {"packet_id": spec.pid, "state": "delivered",
                    "state_zh": "已送达", "plane": PLANE_ZH.get(pk.ver), "switch": None}
        out.append(item)
    return out


class Runner:
    """按给定动作逐拍严格执行，并累积轨迹。供一次性报告与会话步进共用。"""

    def __init__(self, stc: Static):
        self.stc = stc
        self.state = initial_state(stc)
        self.tick = 0
        self.tick_reports: list[dict[str, Any]] = []
        self.buffer_peak = len(self.state.buf)
        self._pid_to_idx = {p.pid: p.index for p in stc.packets}
        self.events: list[list[dict[str, Any]]] = [[] for _ in stc.packets]
        self.released_tick: dict[int, int] = {}
        self.delivered_tick: dict[int, int] = {}

        for p in stc.packets:
            if p.kind == "existing":
                sw = stc.old_path[int(p.init_pos)]
                self.events[p.index].append(
                    {"tick": 0, "type": "preinstalled_position",
                     "switch": sw, "plane": "old",
                     "detail_zh": f"演练开始前已在旧路 {sw}"})
            else:
                self.events[p.index].append(
                    {"tick": int(p.arrival_tick), "type": "scheduled_arrival",
                     "detail_zh": f"预定第 {p.arrival_tick} 拍到达入口"})

    def step(self, action: Action) -> dict[str, Any]:
        tick = self.tick + 1
        before = self.state
        res = advance(self.stc, before, tick, action)
        if not res.ok or res.state is None:
            raise RuntimeError(f"规划动作在第 {tick} 拍严格复放失败：{res.code} {res.detail}")
        st = res.state

        # ---- 轨迹事件 ----
        if res.arrived is not None:
            idx = res.arrived
            self.events[idx].append({"tick": tick, "type": "arrival",
                                     "detail_zh": "到达入口"})
            if idx in st.buf:
                self.events[idx].append(
                    {"tick": tick, "type": "buffered",
                     "detail_zh": f"入口关闭，进入缓冲（当前 {len(st.buf)}/{self.stc.buffer_cap}）"})

        for idx in res.released:
            self.released_tick[idx] = tick
            plane = st.pks[idx].ver
            self.events[idx].append({
                "tick": tick, "type": "release", "plane": PLANE_ZH[plane],
                "path": list(self.stc.paths[plane]),
                "detail_zh": f"入口放行，按此刻生效的{'旧' if plane == 0 else '新'}路走完",
            })

        for hop in res.hops:
            idx = self._pid_to_idx[hop.packet]
            self.events[idx].append({
                "tick": tick, "type": "hop", "at_switch": hop.at_switch,
                "used_nexthop": hop.used_nexthop, "plane": hop.plane,
                "result": hop.result, "to_switch": hop.to_switch,
                "detail_zh": (
                    f"在 {hop.at_switch} 按实装表走 {hop.used_nexthop}"
                    + ("，送达目的地" if hop.result == "delivered" else f"，到 {hop.to_switch}")
                ),
            })
            if hop.result == "delivered":
                self.delivered_tick[idx] = tick

        self.buffer_peak = max(self.buffer_peak, len(st.buf))

        op, plane_t, open_t = action
        report = {
            "tick": tick,
            "table_action": describe_op(op, self.stc),
            "ingress": {
                "plane": PLANE_ZH[plane_t],
                "open": bool(open_t),
                "cutover_this_tick": res.cutover,
                "state_zh": (
                    ("入口开·" + ("新路" if plane_t else "旧路")) if open_t
                    else ("入口关·" + ("新路平面" if plane_t else "旧路平面"))
                ),
            },
            "arrivals": [self.stc.packets[res.arrived].pid] if res.arrived is not None else [],
            "released": [
                {"packet_id": self.stc.packets[i].pid,
                 "plane": PLANE_ZH[st.pks[i].ver]}
                for i in res.released
            ],
            "tables": table_view(self.stc, st),
            "buffer": {
                "used": len(st.buf),
                "capacity": self.stc.buffer_cap,
                "remaining": self.stc.buffer_cap - len(st.buf),
                "packet_ids": [self.stc.packets[i].pid for i in st.buf],
                "overflow": False,
                "usage_zh": f"{len(st.buf)}/{self.stc.buffer_cap}",
            },
            "hops": [
                {
                    "packet_id": hop.packet,
                    "at_switch": hop.at_switch,
                    "plane": hop.plane,
                    "used_nexthop": hop.used_nexthop,
                    "result": hop.result,
                    "to_switch": hop.to_switch,
                }
                for hop in res.hops
            ],
            "packets_after": packet_positions_view(self.stc, st),
            "delivered_this_tick": [
                hop.packet for hop in res.hops if hop.result == "delivered"
            ],
            "goal_reached": goal_reached(self.stc, st),
        }
        self.tick_reports.append(report)
        self.state = st
        self.tick = tick
        return report

    def run(self, actions: tuple[Action, ...] | list[Action]) -> list[dict[str, Any]]:
        for a in actions:
            self.step(a)
        return self.tick_reports

    def trajectories(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        st = self.state
        for spec in self.stc.packets:
            pk = st.pks[spec.index]
            released = self.released_tick.get(spec.index)
            delivered = self.delivered_tick.get(spec.index)
            plane = PLANE_ZH[pk.ver] if pk.ver in (0, 1) else None
            path = list(self.stc.paths[pk.ver]) if pk.ver in (0, 1) else None
            if pk.st == ST_DELIVERED:
                status = "delivered"
            elif pk.st == ST_NET:
                status = "in_network"
            elif pk.st == ST_BUFFERED:
                status = "buffered"
            else:
                status = "not_arrived"
            out.append({
                "packet_id": spec.pid,
                "kind": spec.kind,
                "scheduled_arrival_tick": spec.arrival_tick,
                "initial_switch": (
                    self.stc.old_path[int(spec.init_pos)] if spec.init_pos is not None else None
                ),
                "assigned_plane_at_release": plane,
                "released_tick": released,
                "committed_path": path,
                "delivered_tick": delivered,
                "status": status,
                "events": self.events[spec.index],
            })
        return out

    def summary(self) -> dict[str, Any]:
        st = self.state
        return {
            "executed_ticks": self.tick,
            "goal_reached": goal_reached(self.stc, st),
            "buffer_peak_used": self.buffer_peak,
            "buffer_capacity": self.stc.buffer_cap,
            "final_tables": table_view(self.stc, st),
            "all_delivered": all(p.st == ST_DELIVERED for p in st.pks),
            "ingress_plane": PLANE_ZH[st.plane],
        }
