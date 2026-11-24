"""开发期场景验证 + 搜索规模压测（非 pytest）。"""
import random
import time

from app.domain import ConfigError, build_static
from app.planner import diagnose, find_earliest_plan as find_plan
from app.runner import Runner


def check(name, cfg, expect=None):
    try:
        stc = build_static(**cfg)
    except ConfigError as e:
        print(f"[{name}] CONFIG ERROR: {e.errors}")
        return
    t0 = time.perf_counter()
    r = find_plan(stc)
    dt = (time.perf_counter() - t0) * 1000
    tag = "FEASIBLE" if r.feasible else "INFEASIBLE"
    print(f"[{name}] {tag} nodes={r.nodes} time={dt:.1f}ms goal={len(r.actions) if r.actions else None} cutoff={r.cutoff}")
    if r.feasible:
        runner = Runner(stc)
        runner.run(r.actions)
        assert runner.summary()["goal_reached"], "planner returned plan but runner not at goal"
        print("   actions:", [(a[0][0], a[0][1] if len(a[0]) > 1 else "", f"p{a[1]}o{a[2]}") for a in r.actions])
    elif expect == "infeasible":
        d = diagnose(stc)
        print("   codes:", d["codes"])
        for x in d["reasons_zh"]:
            print("   -", x)
    if expect == "feasible":
        assert r.feasible, f"expected feasible: {name}"
    if expect == "infeasible":
        assert not r.feasible, f"expected infeasible: {name}"


base_kwargs = dict(prefix="p", release_per_tick=None)

# 1. 双版本共存的宽裕场景
check("dual-roomy", {
    **base_kwargs,
    "old_path": ["I", "S1", "S2", "E"],
    "new_path": ["I", "N1", "S2", "E"],
    "table_capacities": {"I": 2, "S1": 1, "S2": 1, "E": 1, "N1": 1},
    "existing_positions": [{"id": "E01", "switch": "S1"}],
    "arrival_ticks": [2, 6],
    "buffer_capacity": 3,
    "deadline": 10,
}, expect="feasible")

# 2. cap=1 必须等在途包离开后 replace
check("cap1-wait-replace", {
    **base_kwargs,
    "old_path": ["I", "S", "A", "E"],
    "new_path": ["I", "S", "B", "E"],
    "table_capacities": {"I": 1, "S": 1, "A": 1, "B": 1, "E": 1},
    "existing_positions": [{"id": "E01", "switch": "S"}],
    "arrival_ticks": [],
    "buffer_capacity": 1,
    "deadline": 6,
}, expect="feasible")

# 3. 期限太短
check("deadline-short", {
    **base_kwargs,
    "old_path": ["I", "S", "A", "E"],
    "new_path": ["I", "S", "B", "E"],
    "table_capacities": {"I": 1, "S": 1, "A": 1, "B": 1, "E": 1},
    "existing_positions": [{"id": "E01", "switch": "S"}],
    "arrival_ticks": [],
    "buffer_capacity": 1,
    "deadline": 2,
}, expect="infeasible")

# 4. 纯传播来不及：第 12 拍到 + 4 台路径
check("arrival-too-late", {
    **base_kwargs,
    "old_path": ["I", "A", "C", "E"],
    "new_path": ["I", "B", "D", "E"],
    "table_capacities": {s: 2 for s in ["I", "A", "B", "C", "D", "E"]},
    "existing_positions": [],
    "arrival_ticks": [12],
    "buffer_capacity": 2,
    "deadline": 12,
}, expect="infeasible")

# 5. 缓冲紧张场景探索
for B in (0, 1, 2):
    check(f"buffer-hunt-B{B}", {
        **base_kwargs,
        "old_path": ["I", "A1", "A2", "E"],
        "new_path": ["I", "B1", "B2", "E"],
        "table_capacities": {s: 1 for s in ["I", "A1", "A2", "B1", "B2", "E"]},
        "existing_positions": [{"id": "E01", "switch": "A2"}, {"id": "E02", "switch": "A1"}],
        "arrival_ticks": [1, 2, 3, 4, 5],
        "buffer_capacity": B,
        "deadline": 12,
    })

# 6. 容量结构性不足：共享分歧机 cap1 且有源源不断旧包 + 短期限
check("cap-conflict", {
    **base_kwargs,
    "old_path": ["I", "S", "A", "E"],
    "new_path": ["I", "S", "B", "E"],
    "table_capacities": {"I": 2, "S": 1, "A": 1, "B": 1, "E": 1},
    "existing_positions": [{"id": "E01", "switch": "S"}],
    "arrival_ticks": [1, 2, 3],
    "buffer_capacity": 8,
    "deadline": 8,
})

# 7. 随机模糊压测：统计节点数/耗时
random.seed(7)
worst = (0, None)
for trial in range(300):
    nsw = random.randint(2, 6)
    sw = [f"S{i}" for i in range(nsw)]
    # 构造同起终两条路径：在中间随机选/排
    mid = sw[1:-1] if nsw >= 2 else []
    random.shuffle(mid)
    cut = random.randint(0, len(mid))
    old_mid = mid[:cut]
    new_mid = [x for x in mid[cut:]]
    # 允许共享部分：随机抽一些同时放两边
    oldp = [sw[0]] + old_mid + [sw[-1]]
    newp = [sw[0]] + new_mid + [sw[-1]]
    if len(oldp) < 2:
        oldp = [sw[0], sw[-1]] if sw[0] != sw[-1] else sw[:2]
    if len(newp) < 2:
        newp = [sw[0], sw[-1]] if sw[0] != sw[-1] else sw[:2]
    union = set(oldp) | set(newp)
    caps = {s: random.choice([1, 1, 2, 3, 6]) for s in union}
    pos = [{"switch": random.choice(oldp[1:-1] or oldp)} for _ in range(random.randint(0, 3))]
    ticks = random.sample(range(1, 13), random.randint(0, 6))
    cfg = {
        **base_kwargs,
        "old_path": oldp, "new_path": newp,
        "table_capacities": caps,
        "existing_positions": pos,
        "arrival_ticks": ticks,
        "buffer_capacity": random.choice([0, 1, 3, 9]),
        "deadline": random.randint(4, 12),
    }
    try:
        stc = build_static(**cfg)
    except ConfigError:
        continue
    t0 = time.perf_counter()
    r = find_plan(stc, node_budget=200000)
    dt = (time.perf_counter() - t0) * 1000
    if r.nodes > worst[0]:
        worst = (r.nodes, (cfg, r.feasible, dt, r.cutoff))
    if r.cutoff or dt > 2000:
        print("SLOW/CUTOFF trial", trial, "nodes", r.nodes, "ms", dt, "feasible", r.feasible)
        print("  cfg", cfg)
print("WORST nodes", worst[0], "ms", worst[1][2] if worst[1] else None,
      "feasible", worst[1][1] if worst[1] else None, "cutoff", worst[1][3] if worst[1] else None)
if worst[1]:
    print("WORST cfg:", worst[1][0])
