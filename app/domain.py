"""领域模型与逐拍状态机（纯 Python，不依赖 FastAPI）。

拍内顺序（与 /docs 和 README 的约定一致）：
  1. 执行至多一条表项操作（增 / 删 / 替换）；
  2. 应用入口控制（开/关，旧平面/新平面，旧→新只允许切一次）；
  3. 本拍到包进入入口队列；
  4. 入口“开”时放行（缺省一次放行全部已到包，可配每拍限量），
     被暂存的包计入缓冲，超过缓冲上限即溢出；
  5. 所有已入网包按“当拍实装表”走一跳。

严格模式（规划器与正式逐拍执行使用）下，包在当前交换机必须能找到
其放行时所在路径版本对应的下一跳表项，否则该迁移被判为非法——
这保证输出的方案中“已入网的包沿放行时生效的路径走完”。
非严格的 what-if 复放在 app.whatif 中，用于演示强改表的真实后果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NamedTuple

# 路径末台交换机把前缀送往直连目的地时使用的虚拟下一跳。
DELIVER = "DELIVER"

MAX_SWITCHES = 6
MAX_TICKS = 12

# 包状态
ST_FUTURE = 3     # 尚未到达
ST_BUFFERED = 0   # 已到、在入口缓冲
ST_NET = 1        # 已入网
ST_DELIVERED = 2  # 已送达

PLANE_OLD = 0
PLANE_NEW = 1


class ConfigError(ValueError):
    """输入配置违反题面硬约束。errors 为全部可读原因。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class PacketSpec:
    index: int
    pid: str
    kind: str           # existing | arrival
    arrival_tick: int | None
    init_pos: int | None  # 旧路序列下标（仅 existing）


@dataclass(frozen=True)
class Static:
    """一次演练中不变的部分（拓扑、容量、包清单）。"""

    prefix: str
    old_path: tuple[str, ...]
    new_path: tuple[str, ...]
    caps: dict[str, int]
    buffer_cap: int
    deadline: int
    packets: tuple[PacketSpec, ...]
    arrivals: dict[int, int]
    release_rate: int | None  # None = 入口开时一次放行全部

    old_nh: dict[str, str] = field(default_factory=dict)
    new_nh: dict[str, str] = field(default_factory=dict)

    @property
    def switches(self) -> list[str]:
        # 保持出现顺序并去重
        seen: set[str] = set()
        out: list[str] = []
        for s in (*self.old_path, *self.new_path):
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out

    @property
    def paths(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return self.old_path, self.new_path

    def nh_maps(self, plane: int) -> dict[str, str]:
        return self.old_nh if plane == PLANE_OLD else self.new_nh


def next_hop_map(path: tuple[str, ...]) -> dict[str, str]:
    return {s: (path[i + 1] if i + 1 < len(path) else DELIVER) for i, s in enumerate(path)}


def validate_raw_config(
    *,
    old_path: list[str],
    new_path: list[str],
    table_capacities: dict[str, int],
    existing_positions: list[dict[str, Any]],
    arrival_ticks: list[int],
    buffer_capacity: int,
    deadline: int,
) -> list[str]:
    """返回中文错误列表；空列表表示通过。"""
    errors: list[str] = []

    def check_path(path: list[str], name: str) -> None:
        if not isinstance(path, list) or len(path) < 2:
            errors.append(f"{name}至少要包含入口和出口两台交换机（长度≥2）")
            return
        if any(not isinstance(s, str) or not s.strip() for s in path):
            errors.append(f"{name}中的交换机名必须是非空字符串")
        if path[0] != path[-1] and len(set(path)) != len(path):
            errors.append(f"{name}存在重复交换机，不是无环路径")
        if path[0] == path[-1]:
            errors.append(f"{name}首尾相同但中间存在环/自环，请给出从入口到出口的无环序列（不要把出口写两次）")

    check_path(old_path, "old_path（旧路）")
    check_path(new_path, "new_path（新路）")

    if not errors or (len(old_path) >= 2 and len(new_path) >= 2):
        if old_path and new_path and old_path[0] != new_path[0]:
            errors.append(f"两条路径必须同起点：旧路入口 {old_path[0]!r}，新路入口 {new_path[0]!r}")
        if old_path and new_path and old_path[-1] != new_path[-1]:
            errors.append(f"两条路径必须同终点：旧路出口 {old_path[-1]!r}，新路出口 {new_path[-1]!r}")

    union = set(old_path) | set(new_path)
    if len(union) > MAX_SWITCHES:
        errors.append(f"两路合计涉及 {len(union)} 台交换机，超过上限 {MAX_SWITCHES} 台")

    if not (1 <= deadline <= MAX_TICKS):
        errors.append(f"完成期限 deadline 必须在 1..{MAX_TICKS} 拍之间（演练最多 {MAX_TICKS} 拍）")
    if buffer_capacity < 0:
        errors.append("入口缓冲上限 buffer_capacity 不能为负")

    for s in union:
        if s not in table_capacities:
            errors.append(f"缺少交换机 {s!r} 的表项上限 table_capacities[{s}]")
        else:
            cap = table_capacities[s]
            if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
                errors.append(f"table_capacities[{s!r}] 必须是非负整数")

    # 起初只装旧路：每台旧路交换机至少要装 1 条旧路表项
    for s in old_path:
        cap = table_capacities.get(s)
        if isinstance(cap, int) and not isinstance(cap, bool) and cap < 1:
            errors.append(f"交换机 {s!r} 的表项上限为 {cap}，但起初就要安装旧路表项，至少为 1")

    old_set = set(old_path)
    for i, p in enumerate(existing_positions):
        sw = p.get("switch") if isinstance(p, dict) else None
        if not isinstance(sw, str) or not sw.strip():
            errors.append(f"第 {i + 1} 个存量包缺少有效的 switch 位置")
        elif sw not in old_set:
            errors.append(f"存量包 {p.get('id') or i + 1} 的位置 {sw!r} 不在旧路 {old_path} 上")

    seen_ticks: set[int] = set()
    for t in arrival_ticks:
        if not isinstance(t, int) or isinstance(t, bool):
            errors.append(f"到达时刻 {t!r} 不是整数拍")
            continue
        if not (1 <= t <= MAX_TICKS):
            errors.append(f"到达时刻 {t} 超出 1..{MAX_TICKS} 拍")
        if t in seen_ticks:
            errors.append(f"第 {t} 拍有多个包到达，但每拍最多到 1 个包")
        seen_ticks.add(t)

    return errors


def build_static(
    *,
    prefix: str,
    old_path: list[str],
    new_path: list[str],
    table_capacities: dict[str, int],
    existing_positions: list[dict[str, Any]],
    arrival_ticks: list[int],
    buffer_capacity: int,
    deadline: int,
    release_per_tick: int | None = None,
) -> Static:
    errors = validate_raw_config(
        old_path=old_path,
        new_path=new_path,
        table_capacities=table_capacities,
        existing_positions=existing_positions,
        arrival_ticks=arrival_ticks,
        buffer_capacity=buffer_capacity,
        deadline=deadline,
    )
    if errors:
        raise ConfigError(errors)

    packets: list[PacketSpec] = []
    used_ids: set[str] = set()

    def unique_id(candidate: str) -> str:
        if candidate not in used_ids:
            used_ids.add(candidate)
            return candidate
        k = 2
        while f"{candidate}-{k}" in used_ids:
            k += 1
        final = f"{candidate}-{k}"
        used_ids.add(final)
        return final

    old_index = {s: i for i, s in enumerate(old_path)}
    for i, pos in enumerate(existing_positions):
        raw_id = pos.get("id")
        pid = unique_id(str(raw_id) if raw_id else f"E{i + 1:02d}")
        packets.append(
            PacketSpec(
                index=i,
                pid=pid,
                kind="existing",
                arrival_tick=None,
                init_pos=old_index[pos["switch"]],
            )
        )

    arrivals: dict[int, int] = {}
    base = len(packets)
    for j, t in enumerate(sorted(arrival_ticks)):
        idx = base + j
        pid = unique_id(f"A{t}")
        packets.append(
            PacketSpec(index=idx, pid=pid, kind="arrival", arrival_tick=t, init_pos=None)
        )
        arrivals[t] = idx

    return Static(
        prefix=prefix,
        old_path=tuple(old_path),
        new_path=tuple(new_path),
        caps={s: int(table_capacities[s]) for s in dict.fromkeys([*old_path, *new_path])},
        buffer_cap=buffer_capacity,
        deadline=deadline,
        packets=tuple(packets),
        arrivals=arrivals,
        release_rate=release_per_tick,
        old_nh=next_hop_map(tuple(old_path)),
        new_nh=next_hop_map(tuple(new_path)),
    )


# ---------------------------------------------------------------------------
# 紧凑可哈希状态（供规划器记忆化搜索）
# ---------------------------------------------------------------------------


class Pk(NamedTuple):
    st: int   # ST_*
    ver: int  # -1 / PLANE_OLD / PLANE_NEW
    pos: int
    rel: int


class State(NamedTuple):
    items: frozenset[tuple[str, str]]
    plane: int
    opened: int
    buf: tuple[int, ...]
    pks: tuple[Pk, ...]


# 表项操作：
#  ("noop",)
#  ("add", switch, nexthop)
#  ("del", switch, nexthop)
#  ("rep", switch, old_nexthop, new_nexthop)
Op = tuple[Any, ...]
# 完整一拍决策：(op, target_plane, target_opened)
Action = tuple[Op, int, int]


def initial_state(stc: Static) -> State:
    items = frozenset((s, stc.old_nh[s]) for s in stc.old_path)
    pks: list[Pk] = []
    for p in stc.packets:
        if p.kind == "existing":
            pks.append(Pk(ST_NET, PLANE_OLD, int(p.init_pos), 0))
        else:
            pks.append(Pk(ST_FUTURE, -1, -1, -1))
    return State(items=items, plane=PLANE_OLD, opened=1, buf=(), pks=tuple(pks))


def old_item_is_exclusive(stc: Static, s: str) -> bool:
    """旧路在 s 上的下一跳表项是否为“旧路专用”（新路在同机没有同一个下一跳）。"""
    if s not in stc.old_nh:
        return False
    return not (s in stc.new_nh and stc.new_nh[s] == stc.old_nh[s])


def goal_reached(stc: Static, state: State) -> bool:
    if any(p.st != ST_DELIVERED for p in state.pks):
        return False
    if state.plane != PLANE_NEW:
        return False
    # 新路整条已实装
    if any((s, nh) not in state.items for s, nh in stc.new_nh.items()):
        return False
    # 旧路专用表项撤净（新旧同下一跳的共享项不算旧路专用，可保留）
    for s, nh in stc.old_nh.items():
        if old_item_is_exclusive(stc, s) and (s, nh) in state.items:
            return False
    return True


def min_ops_remaining(stc: Static, state: State) -> int:
    """达成每台交换机目标表态至少还需要几次单机操作（每次只动一台）。

    同机“新路下一跳缺失 + 旧路专用项还在且两者不同”可由一次 replace 同时完成，
    因此每台机器的下界是 0 或 1。该下界可采纳，用于剪枝。
    """
    n = 0
    for s in stc.switches:
        need_new = s in stc.new_nh and (s, stc.new_nh[s]) not in state.items
        old_bad = (
            s in stc.old_nh
            and old_item_is_exclusive(stc, s)
            and (s, stc.old_nh[s]) in state.items
        )
        if need_new or old_bad:
            n += 1
    return n


def earliest_delivery_bound_ok(stc: Static, state: State, elapsed_ticks: int) -> bool:
    """截止期可行性剪枝：按最顺利的放行/选路估算，是否还可能在 deadline 前送达。"""
    D = stc.deadline
    min_path_len = min(len(stc.old_path), len(stc.new_path))

    for p, pk in zip(stc.packets, state.pks):
        if pk.st == ST_DELIVERED:
            continue
        if pk.st == ST_FUTURE:
            a = int(p.arrival_tick)  # type: ignore[arg-type]
            if a + min_path_len - 1 > D:
                return False
        elif pk.st == ST_BUFFERED:
            # 最早下一拍放行；放行拍做第一次查找，故最早送达 elapsed + len(path)
            if elapsed_ticks + min_path_len > D:
                return False
        elif pk.st == ST_NET:
            path = stc.paths[pk.ver]
            remaining_lookups = len(path) - pk.pos
            if elapsed_ticks + remaining_lookups > D:
                return False
    return True


def static_deadline_lower_bound(stc: Static) -> tuple[int, list[str]]:
    """不考虑表项/操作冲突时，纯传播所需的最早完成拍与逐包结论。"""
    worst = 0
    notes: list[str] = []
    m_old, m_new = len(stc.old_path), len(stc.new_path)
    old_index = {s: i for i, s in enumerate(stc.old_path)}
    for p in stc.packets:
        if p.kind == "existing":
            i = int(p.init_pos)
            deliver_tick = m_old - i
            notes.append(f"{p.pid} 演练前位于旧路 {stc.old_path[i]}，最快第 {deliver_tick} 拍送达")
        else:
            a = int(p.arrival_tick)
            deliver_tick = a + min(m_old, m_new) - 1
            notes.append(f"{p.pid} 第 {a} 拍到，最快第 {deliver_tick} 拍送达")
        worst = max(worst, deliver_tick)
    return worst, notes


def candidate_ops(stc: Static, state: State) -> list[Op]:
    """当前表态下所有“看起来朝目标推进”的合法候选操作（noop 兜底放最后）。"""
    ops: list[Op] = []
    items = state.items

    adds: list[Op] = []
    reps: list[Op] = []
    dels: list[Op] = []

    for s in stc.switches:
        old_nh = stc.old_nh.get(s)
        new_nh = stc.new_nh.get(s)
        has_old = old_nh is not None and (s, old_nh) in items
        has_new = new_nh is not None and (s, new_nh) in items

        # 安装新路所需下一跳（新旧相同则旧项已兼作新项，无需操作）
        if new_nh is not None and not has_new:
            adds.append(("add", s, new_nh))

        # 同机旧项下一跳 -> 新项下一跳的原子替换（容量紧张时的关键手段）
        if (
            old_nh is not None
            and new_nh is not None
            and old_nh != new_nh
            and has_old
            and not has_new
        ):
            reps.append(("rep", s, old_nh, new_nh))

        # 撤旧路专用项
        if old_nh is not None and old_item_is_exclusive(stc, s) and has_old:
            dels.append(("del", s, old_nh))

    # 顺序影响 DFS 找到首个解的速度与“观感”：先铺路、再替换、最后撤旧，noop 兜底。
    ops.extend(adds)
    ops.extend(reps)
    ops.extend(dels)
    ops.append(("noop",))
    return ops


def candidate_ingress_targets(stc: Static, state: State) -> list[tuple[int, int]]:
    """枚举全部合法入口目标（保证搜索完备），只调整先后顺序以优先得到“像样”的方案。"""
    if state.plane == PLANE_OLD:
        new_ready = all((s, nh) in state.items for s, nh in stc.new_nh.items())
        if new_ready:
            # 新路已整条装好：教科书动作是立刻切新路并保持放行
            return [(PLANE_NEW, 1), (PLANE_OLD, 1), (PLANE_NEW, 0), (PLANE_OLD, 0)]
        # 常规：继续开旧路 -> 同拍切新并开 -> 关旧暂存 -> 切新但先关
        return [(PLANE_OLD, 1), (PLANE_NEW, 1), (PLANE_OLD, 0), (PLANE_NEW, 0)]
    return [(PLANE_NEW, 1), (PLANE_NEW, 0)]


def candidate_actions(stc: Static, state: State) -> list[Action]:
    actions: list[Action] = []
    for op in candidate_ops(stc, state):
        for plane_t, open_t in candidate_ingress_targets(stc, state):
            actions.append((op, plane_t, open_t))
    return actions


@dataclass
class HopEvent:
    packet: str
    at_switch: str
    plane: str
    used_nexthop: str
    result: str          # forwarded | delivered
    to_switch: str | None = None


@dataclass
class StepResult:
    ok: bool
    state: State | None = None
    code: str = ""
    detail: str = ""
    hops: list[HopEvent] = field(default_factory=list)
    released: list[int] = field(default_factory=list)
    arrived: int | None = None
    cutover: bool = False


def _reject(code: str, detail: str) -> StepResult:
    return StepResult(ok=False, code=code, detail=detail)


def advance(
    stc: Static,
    state: State,
    tick: int,
    action: Action,
) -> StepResult:
    """严格推进一拍。任何溢出、超表容、在途包找不到放行路径表项都拒绝。"""
    op, plane_t, open_t = action

    # ---- 1. 表项操作 -----------------------------------------------------
    items = set(state.items)
    kind = op[0]
    if kind == "add":
        _, s, nh = op
        if (s, nh) in items:
            return _reject("op_redundant", f"{s} 已存在下一跳 {nh} 的表项")
        if sum(1 for ss, _ in items if ss == s) + 1 > stc.caps[s]:
            return _reject("table_full", f"{s} 表项上限 {stc.caps[s]}，新增 {nh} 会超限")
        items.add((s, nh))
    elif kind == "del":
        _, s, nh = op
        if (s, nh) not in items:
            return _reject("entry_missing", f"{s} 不存在下一跳 {nh} 的表项，无法删除")
        items.discard((s, nh))
    elif kind == "rep":
        _, s, old_nh, new_nh = op
        if (s, old_nh) not in items:
            return _reject("entry_missing", f"{s} 旧下一跳 {old_nh} 表项不在，无法替换")
        if (s, new_nh) in items:
            return _reject("op_redundant", f"{s} 下一跳 {new_nh} 表项已在，无需替换")
        items.discard((s, old_nh))
        items.add((s, new_nh))
        if sum(1 for ss, _ in items if ss == s) > stc.caps[s]:
            return _reject("table_full", f"{s} 替换后超过表项上限 {stc.caps[s]}")
    elif kind != "noop":
        return _reject("bad_op", f"未知表项操作 {kind!r}")

    # ---- 2. 入口控制（平面不允许回切） -----------------------------------
    if plane_t < state.plane:
        return _reject("cutback_forbidden", "入口只能从旧路切到新路一次，不能回切")
    cutover = plane_t == PLANE_NEW and state.plane == PLANE_OLD

    # ---- 3. 到包 ---------------------------------------------------------
    pks = list(state.pks)
    buf = list(state.buf)
    arrived_idx: int | None = None
    idx = stc.arrivals.get(tick)
    if idx is not None:
        if pks[idx].st != ST_FUTURE:
            return _reject("arrival_state", f"第 {tick} 拍到包状态异常")
        pks[idx] = Pk(ST_BUFFERED, -1, -1, -1)
        buf.append(idx)
        arrived_idx = idx

    # ---- 4. 放行 / 缓冲 --------------------------------------------------
    released: list[int] = []
    if open_t == 1 and buf:
        if stc.release_rate is None:
            take_n = len(buf)
        else:
            take_n = max(0, min(int(stc.release_rate), len(buf)))
        take = buf[:take_n]
        buf = buf[take_n:]
        for i in take:
            pks[i] = Pk(ST_NET, plane_t, 0, tick)
            released.append(i)

    if len(buf) > stc.buffer_cap:
        return _reject(
            "buffer_overflow",
            f"第 {tick} 拍入口缓冲 {len(buf)} 个包，超过上限 {stc.buffer_cap}",
        )

    # 开着的入口必须还装着其所选平面的首跳表项，否则“open + 该平面”是自相矛盾的状态。
    ingress = stc.old_path[0]
    if open_t == 1 and (ingress, stc.nh_maps(plane_t)[ingress]) not in items:
        return _reject(
            "open_plane_entry_missing",
            f"第 {tick} 拍入口为开且选择{'新' if plane_t else '旧'}平面，"
            f"但入口机 {ingress} 上对应的首跳表项不在",
        )

    # ---- 5. 已入网包走一跳（严格保证仍在放行时路径上） -------------------
    hops: list[HopEvent] = []
    for i, pk in enumerate(pks):
        if pk.st != ST_NET:
            continue
        path = stc.paths[pk.ver]
        if not (0 <= pk.pos < len(path)):
            return _reject("bad_position", f"{stc.packets[i].pid} 位置越界")
        s = path[pk.pos]
        req_nh = stc.nh_maps(pk.ver)[s]
        if (s, req_nh) not in items:
            plane_name = "旧" if pk.ver == PLANE_OLD else "新"
            return _reject(
                "in_flight_path_broken",
                f"{stc.packets[i].pid} 是在{ '旧' if pk.ver == 0 else '新'}路放行的包，"
                f"第 {tick} 拍到达 {s} 时，{plane_name}路径所需下一跳 {req_nh} 的表项已不在实装表中",
            )
        if req_nh == DELIVER:
            hops.append(HopEvent(stc.packets[i].pid, s, "old" if pk.ver == 0 else "new",
                                 req_nh, "delivered", None))
            pks[i] = Pk(ST_DELIVERED, pk.ver, -2, pk.rel)
        else:
            hops.append(HopEvent(stc.packets[i].pid, s, "old" if pk.ver == 0 else "new",
                                 req_nh, "forwarded", req_nh))
            pks[i] = Pk(ST_NET, pk.ver, pk.pos + 1, pk.rel)

    new_state = State(
        items=frozenset(items),
        plane=plane_t,
        opened=open_t,
        buf=tuple(buf),
        pks=tuple(pks),
    )
    return StepResult(ok=True, state=new_state, hops=hops, released=released,
                      arrived=arrived_idx, cutover=cutover)
