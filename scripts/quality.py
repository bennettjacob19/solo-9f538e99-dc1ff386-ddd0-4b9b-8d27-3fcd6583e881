"""验证 plan_best 的方案质量、性能，并寻找缓冲存在性约束。"""
import random
import time
from dataclasses import replace

from app.domain import build_static, ConfigError
from app.planner import plan_best, diagnose
from app.runner import Runner


def show(name, cfg, diag_on_fail=True):
    stc = build_static(**cfg)
    t0 = time.perf_counter()
    r = plan_best(stc)
    ms = (time.perf_counter() - t0) * 1000
    if r.feasible:
        rn = Runner(stc); rn.run(r.actions)
        nnew = sum(1 for p in rn.state.pks if p.ver == 1)
        print(f"[{name}] FEASIBLE goal={len(r.actions)} nodes={r.nodes} {ms:.0f}ms "
              f"newpkts={nnew} peakbuf={rn.buffer_peak}")
        for i, a in enumerate(r.actions, 1):
            print("   ", i, a[0], f"plane={a[1]} open={a[2]}")
    else:
        print(f"[{name}] INFEASIBLE nodes={r.nodes} {ms:.0f}ms cutoff={r.cutoff}")
        if diag_on_fail:
            d = diagnose(stc)
            for c, x in zip(d["codes"], d["reasons_zh"]):
                print("    *", c, "|", x[:110])
    return r


K = dict(prefix="p", release_per_tick=None)

show("dual-roomy", {**K,
    "old_path": ["I", "S1", "S2", "E"], "new_path": ["I", "N1", "S2", "E"],
    "table_capacities": {"I": 2, "S1": 1, "S2": 1, "E": 1, "N1": 1},
    "existing_positions": [{"switch": "S1"}], "arrival_ticks": [2, 6],
    "buffer_capacity": 3, "deadline": 10})

# 对抗：旧 I-U-V-S-E(5)，新 I-S-N-E(4)，S 旧index3 新index1，全 cap1，连续到 9
topo6 = dict(old_path=["I", "U", "V", "S", "E"], new_path=["I", "S", "N", "E"],
             table_capacities={s: 1 for s in ["I", "U", "V", "S", "N", "E"]})
for arr in ([5, 6, 7, 8, 9], [6, 7, 8, 9], [7, 8, 9]):
    for B in (0, 1, 2, 3, 9):
        r = show(f"adv arr{arr[0]}..{arr[-1]} B={B} D=12",
                 {**K, **topo6, "existing_positions": [], "arrival_ticks": arr,
                  "buffer_capacity": B, "deadline": 12}, diag_on_fail=(B == 0))
    print()

# 随机性能/正确性
random.seed(11)
worst = (0, None)
infeas = 0
t0 = time.perf_counter()
for trial in range(1500):
    n = random.randint(2, 6)
    sw = [f"S{i}" for i in range(n)]
    a, z = sw[0], sw[-1]
    middle = sw[1:-1]; random.shuffle(middle)
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
    cfg = {**K, "old_path": oldp, "new_path": newp,
           "table_capacities": {s: random.choice([1, 1, 2, 3, 6]) for s in union},
           "existing_positions": [{"switch": random.choice(oldp[1:])} for _ in range(random.randint(0, 3))],
           "arrival_ticks": random.sample(range(1, 13), random.randint(0, 8)),
           "buffer_capacity": random.choice([0, 1, 2, 4, 9]),
           "deadline": random.randint(6, 12)}
    try:
        stc = build_static(**cfg)
    except ConfigError:
        continue
    r = plan_best(stc, node_budget=500000)
    if not r.feasible:
        infeas += 1
    if r.nodes > worst[0]:
        worst = (r.nodes, cfg)
    if r.feasible:
        rn = Runner(stc); rn.run(r.actions)
        assert rn.summary()["goal_reached"]
    if r.cutoff:
        print("CUTOFF", cfg)
print(f"fuzz done 1500 in {time.perf_counter()-t0:.1f}s infeasible={infeas} worst_nodes={worst[0]}")
print("worst cfg:", worst[1])
