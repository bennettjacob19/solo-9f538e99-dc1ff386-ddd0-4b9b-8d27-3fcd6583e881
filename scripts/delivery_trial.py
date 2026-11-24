"""交付前试跑：在途包遇上改表。

对运行中的服务（默认 http://127.0.0.1:8000，可用 BASE_URL 覆盖）发起真实 HTTP：
  1) 严格规划器给出的方案必须等在途包离开 S 后才替换 S；
  2) what-if 故意在第 1 拍替换/删除 S，复现改道 / 黑洞。
退出码 0 表示全部断言通过。
"""

from __future__ import annotations

import os
import sys

import httpx

from app.examples import (
    FEASIBLE_REPLACE_WAIT,
    INFLIGHT_DELETE_WHATIF,
    INFLIGHT_REPLACE_BLACKHOLE,
    INFLIGHT_WHATIF,
)

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")


def main() -> int:
    failures: list[str] = []
    with httpx.Client(base_url=BASE, timeout=10) as c:
        h = c.get("/health").json()
        print(f"[health] {h}")

        # ---- 1. 安全方案 ----
        d = c.post("/api/v1/plans", json=FEASIBLE_REPLACE_WAIT).json()
        if d["status"] != "feasible":
            failures.append(f"安全场景应当有解，实际 {d['status']}: {d.get('diagnosis')}")
        else:
            replace_tick = None
            s_hop_tick = None
            for t in d["ticks"]:
                a = t["table_action"]
                if a["kind"] == "replace" and a["switch"] == "S":
                    replace_tick = t["tick"]
                for hop in t["hops"]:
                    if hop["packet_id"] == "E01" and hop["at_switch"] == "S":
                        s_hop_tick = t["tick"]
            e01 = next(p for p in d["packet_trajectories"] if p["packet_id"] == "E01")
            print(f"[safe] goal_tick={d['goal_tick']} E01={e01['status']} "
                  f"E01 在 S 起跳拍={s_hop_tick} 替换 S 拍={replace_tick}")
            print("[safe] 动作序列:",
                  [(t["tick"], t["table_action"]["kind"], t["table_action"].get("switch"))
                   for t in d["ticks"] if t["table_action"]["kind"] != "noop"])
            if not (s_hop_tick == 1 and replace_tick and replace_tick > s_hop_tick):
                failures.append("安全方案没有保证 E01 离开 S 后才替换 S")
            if e01["status"] != "delivered":
                failures.append("E01 未送达")

        # ---- 2. what-if replace ----
        w = c.post("/api/v1/whatif", json=INFLIGHT_WHATIF).json()
        hit = [v for v in w["violations"] if v["code"] == "in_flight_hit_changed_table"]
        traj = next(p for p in w["packet_trajectories"] if p["packet_id"] == "E01")
        print(f"[replace] 碰撞事件 {len(hit)} 条；E01 终态={traj['final_state']} "
              f"途经={traj['visited_switches']}")
        for v in hit[:1]:
            print("           ", v["detail_zh"])
        if not hit or "B" not in traj["visited_switches"]:
            failures.append("replace what-if 未复现碰撞/改道")

        # ---- 2b. what-if replace 后不补新路表 -> 改道后黑洞 ----
        wb = c.post("/api/v1/whatif", json=INFLIGHT_REPLACE_BLACKHOLE).json()
        trajb = next(p for p in wb["packet_trajectories"] if p["packet_id"] == "E01")
        print(f"[replace->blackhole] E01 终态={trajb['final_state']} 途经={trajb['visited_switches']}")
        if trajb["final_state"] != "dropped" or trajb["visited_switches"][:2] != ["S", "B"]:
            failures.append("replace->blackhole what-if 未复现改道后丢弃")

        # ---- 3. what-if delete ----
        wd = c.post("/api/v1/whatif", json=INFLIGHT_DELETE_WHATIF).json()
        trajd = next(p for p in wd["packet_trajectories"] if p["packet_id"] == "E01")
        print(f"[delete]  E01 终态={trajd['final_state']}；违规码="
              f"{[v['code'] for v in wd['violations']]}")
        if trajd["final_state"] != "dropped":
            failures.append("delete what-if 未复现黑洞丢包")

    if failures:
        print("\nTRIAL FAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nTRIAL PASS: 在途包改表的护栏与破坏后果均已实测符合预期。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
