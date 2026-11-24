"""定向构造缓冲/表容冲突：共享分歧机 S，旧路上游 2 跳、新路上游 1 跳。"""
from dataclasses import replace

from app.domain import build_static
from app.planner import diagnose, find_earliest_plan
from app.runner import Runner


def trial(name, oldp, newp, caps, arr, B, D, existing=None):
    cfg = dict(prefix="p", old_path=oldp, new_path=newp, table_capacities=caps,
               existing_positions=existing or [], arrival_ticks=arr,
               buffer_capacity=B, deadline=D, release_per_tick=None)
    stc = build_static(**cfg)
    r = find_earliest_plan(stc)
    print(f"== {name}: feasible={r.feasible} nodes={r.nodes} goal={len(r.actions) if r.actions else '-'}")
    if r.feasible:
        for i, a in enumerate(r.actions, 1):
            print("   ", i, a[0], "plane", a[1], "open", a[2])
        rn = Runner(stc); rn.run(r.actions)
        # 统计走新平面的包
        nnew = sum(1 for p in rn.state.pks if p.ver == 1)
        print("    new-plane packets:", nnew, "peak buffer:", rn.buffer_peak)
    else:
        d = diagnose(stc)
        print("    codes:", d["codes"], "minops:", d["minimum_table_ops"])
        for x in d["reasons_zh"]:
            print("    -", x)
    return stc, r


# 旧 I-U-S-E（S 在 index2），新 I-S-N-E（S 在 index1）；S cap1，全部 cap1
oldp = ["I", "U", "S", "E"]
newp = ["I", "S", "N", "E"]
caps1 = {"I": 1, "U": 1, "S": 1, "N": 1, "E": 1}
arr_full = list(range(1, 10))

for B in (0, 1, 2, 4, 12):
    trial(f"saturated B={B} D=12", oldp, newp, caps1, arr_full, B, 12)

print()
caps2 = dict(caps1); caps2["S"] = 2; caps2["I"] = 2
trial("dual-cap I/S=2 B=0", oldp, newp, caps2, arr_full, 0, 12)

print()
# 较短期限 + 稀疏到包
trial("sparse B=0 D=8", oldp, newp, caps1, [1, 4, 7], 0, 8)
trial("sparse B=1 D=8", oldp, newp, caps1, [1, 4, 7], 1, 8)
