"""寻找能分别命中 缓冲/表容/期限 约束的具体配置，并压测最坏搜索规模。"""
import random
import time
from dataclasses import replace

from app.domain import build_static, ConfigError
from app.planner import find_earliest_plan


def feasible(cfg, **relax):
    stc = build_static(**cfg)
    if relax.get("big_caps"):
        stc = replace(stc, caps={s: 64 for s in stc.caps})
    if relax.get("inf_buffer"):
        stc = replace(stc, buffer_cap=10**6)
    if relax.get("deadline12"):
        stc = replace(stc, deadline=12)
    r = find_earliest_plan(stc, node_budget=300000)
    return r.feasible, r


random.seed(20261124)
found_buffer = None
found_table = None
worst_nodes = 0
worst_cfg = None
t_start = time.perf_counter()

for trial in range(4000):
    n = random.randint(3, 6)
    sw = [f"S{i}" for i in range(n)]
    a, z = sw[0], sw[-1]
    middle = sw[1:-1]
    random.shuffle(middle)
    # 随机把中间机分成 old/new，允许 1 台共享分歧机
    if len(middle) >= 2 and random.random() < 0.6:
        shared = [middle.pop(random.randrange(len(middle)))]
    else:
        shared = []
    cut = random.randint(0, len(middle))
    old_mid = middle[:cut] + shared
    new_mid = middle[cut:] + shared
    random.shuffle(old_mid)
    random.shuffle(new_mid)
    oldp = [a] + old_mid + [z]
    newp = [a] + new_mid + [z]
    if len(set(oldp)) != len(oldp) or len(set(newp)) != len(newp):
        continue
    union = set(oldp) | set(newp)
    if len(union) > 6:
        continue

    cap_choice = random.choice([1, 1, 1, 2, 2, 3])
    caps = {s: (1 if random.random() < 0.55 else cap_choice) for s in union}
    if random.random() < 0.3:
        caps[random.choice(list(union))] = 1

    npack_existing = random.randint(0, 3)
    pos = [{"switch": random.choice(oldp[1:])} for _ in range(npack_existing)]
    arr = sorted(random.sample(range(1, 13), random.randint(1, 8)))
    B = random.choice([0, 0, 1, 1, 2, 3, 5])
    D = random.randint(5, 12)
    cfg = dict(prefix="p", old_path=oldp, new_path=newp, table_capacities=caps,
               existing_positions=pos, arrival_ticks=arr, buffer_capacity=B,
               deadline=D, release_per_tick=None)
    try:
        stc = build_static(**cfg)
    except ConfigError:
        continue

    r0 = find_earliest_plan(stc, node_budget=200000)
    worst_nodes = max(worst_nodes, r0.nodes)
    if r0.nodes == worst_nodes:
        worst_cfg = (cfg, r0.feasible, r0.nodes)
    if r0.feasible:
        continue

    fb, rb = feasible(cfg, inf_buffer=True)
    ft, rt = feasible(cfg, big_caps=True)
    f12, r12 = feasible(cfg, deadline12=True)
    if found_buffer is None and fb and not ft:
        found_buffer = (cfg, rb.nodes)
        print("BUFFER-BINDING FOUND:", cfg)
    if found_table is None and ft and not fb:
        found_table = (cfg, rt.nodes)
        print("TABLE-BINDING FOUND:", cfg)
    if found_buffer and found_table:
        break

print("elapsed total ms", round((time.perf_counter() - t_start) * 1000))
print("worst nodes", worst_nodes)
if worst_cfg:
    print("worst feasible/nodes:", worst_cfg[1], worst_cfg[2])
    print("worst cfg:", worst_cfg[0])
print("buffer example:", found_buffer[0] if found_buffer else None)
print("table example:", found_table[0] if found_table else None)
