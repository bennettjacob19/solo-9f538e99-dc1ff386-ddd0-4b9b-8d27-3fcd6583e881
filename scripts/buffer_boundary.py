"""验证缓冲刚性边界：饱和到包 + 共享分歧机 + 新路更近，比较 B=0..3 的存在性。"""
from app.domain import build_static
from app.planner import find_plan, plan_best
from app.runner import Runner

K = dict(prefix="p", release_per_tick=None)
topo6 = dict(old_path=["I", "U", "V", "S", "E"], new_path=["I", "S", "N", "E"],
             table_capacities={s: 1 for s in ["I", "U", "V", "S", "N", "E"]})

for arr_name, arr in [("sat1-9", list(range(1, 10))), ("cons5-9", [5, 6, 7, 8, 9])]:
    for B in (0, 1, 2, 3, 4):
        cfg = {**K, **topo6, "existing_positions": [], "arrival_ticks": arr,
               "buffer_capacity": B, "deadline": 12}
        stc = build_static(**cfg)
        r = find_plan(stc)
        line = f"{arr_name} B={B}: feasible={r.feasible} nodes={r.nodes} cutoff={r.cutoff}"
        if r.feasible:
            rn = Runner(stc); rn.run(r.actions)
            line += f" goal={len(r.actions)} peakbuf={rn.buffer_peak}"
        print(line)
    print()

# 加存量包的更狠版本：E01@V, E02@S
for B in (0, 1, 2, 3):
    cfg = {**K, **topo6,
           "existing_positions": [{"id": "E01", "switch": "V"}, {"id": "E02", "switch": "S"}],
           "arrival_ticks": list(range(1, 10)), "buffer_capacity": B, "deadline": 12}
    stc = build_static(**cfg)
    r = find_plan(stc)
    line = f"existing2 sat B={B}: feasible={r.feasible} nodes={r.nodes}"
    if r.feasible:
        rn = Runner(stc); rn.run(r.actions)
        line += f" goal={len(r.actions)} peakbuf={rn.buffer_peak}"
    print(line)
