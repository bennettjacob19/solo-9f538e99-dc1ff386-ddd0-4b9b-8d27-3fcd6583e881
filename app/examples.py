"""预置演练场景：/docs 一键填充、GET /api/v1/examples、测试共用。"""

from __future__ import annotations

from typing import Any

# 1) 宽裕双版本：入口 I 容量 2，可同时挂旧/新两个下一跳；含存量包与后续到包。
FEASIBLE_DUAL: dict[str, Any] = {
    "prefix": "203.0.113.0/24",
    "old_path": ["I", "S1", "S2", "E"],
    "new_path": ["I", "N1", "S2", "E"],
    "table_capacities": {"I": 2, "S1": 1, "N1": 1, "S2": 1, "E": 1},
    "existing_packets": [{"id": "E01", "switch": "S1"}],
    "arrival_ticks": [2, 6],
    "buffer_capacity": 3,
    "deadline": 10,
}

# 2) 容量=1 必须“等在途包离开 -> 原子替换”的紧凑场景（无后续包也能完成切换撤旧）。
FEASIBLE_REPLACE_WAIT: dict[str, Any] = {
    "prefix": "198.51.100.0/24",
    "old_path": ["I", "S", "A", "E"],
    "new_path": ["I", "S", "B", "E"],
    "table_capacities": {"I": 1, "S": 1, "A": 1, "B": 1, "E": 1},
    "existing_packets": [{"id": "E01", "switch": "S"}],
    "arrival_ticks": [],
    "buffer_capacity": 1,
    "deadline": 6,
}

# 3) 缓冲边界（可解）：旧路 I-U-V-S-E、新路 I-S-N-E，S 是共享分歧机且容量 1。
#    两台存量包压在 V/S，1..9 拍连续到包；B=1 时搜索找到“关一拍错峰”的方案。
BUFFER_BOUNDARY_OK: dict[str, Any] = {
    "prefix": "192.0.2.0/24",
    "old_path": ["I", "U", "V", "S", "E"],
    "new_path": ["I", "S", "N", "E"],
    "table_capacities": {"I": 1, "U": 1, "V": 1, "S": 1, "N": 1, "E": 1},
    "existing_packets": [
        {"id": "E01", "switch": "V"},
        {"id": "E02", "switch": "S"},
    ],
    "arrival_ticks": [1, 2, 3, 4, 5, 6, 7, 8, 9],
    "buffer_capacity": 1,
    "deadline": 12,
}

# 4) 同一冲突把缓冲改成 0：无解；归因会指出放宽缓冲或表容可解，旧表照用。
BUFFER_ZERO_INFEASIBLE: dict[str, Any] = {
    **BUFFER_BOUNDARY_OK,
    "buffer_capacity": 0,
}

# 5) 纯期限不够：第 12 拍才到的包，任一路都要 4 拍，第 12 拍期限内不可能送达。
DEADLINE_INFEASIBLE: dict[str, Any] = {
    "prefix": "203.0.113.7/32",
    "old_path": ["I", "A", "C", "E"],
    "new_path": ["I", "B", "D", "E"],
    "table_capacities": {s: 2 for s in ["I", "A", "B", "C", "D", "E"]},
    "existing_packets": [],
    "arrival_ticks": [12],
    "buffer_capacity": 4,
    "deadline": 12,
}

# 6) What-if：存量包 E01 此刻就在共享分歧机 S，第 1 拍直接 replace S 的下一跳。
#    第 2 拍又把新路 B→E 补齐，于是旧包被静默改道、最终“偏路径送达”——
#    包没丢，但违反了“沿放行时路径走完”，在真实运维里属于绕过审计路径。
INFLIGHT_WHATIF: dict[str, Any] = {
    "drill": {
        "prefix": "198.51.100.0/24",
        "old_path": ["I", "S", "A", "E"],
        "new_path": ["I", "S", "B", "E"],
        "table_capacities": {"I": 1, "S": 1, "A": 1, "B": 1, "E": 1},
        "existing_packets": [{"id": "E01", "switch": "S"}],
        "arrival_ticks": [],
        "buffer_capacity": 1,
        "deadline": 4,
    },
    "script": [
        {"tick": 1, "action": "replace", "switch": "S"},
        {"tick": 2, "action": "add", "switch": "B"},
    ],
    "cutover_tick": None,
    "closed_ticks": [],
    "replay_ticks": 3,
}

# 6b) What-if：第 1 拍 replace S 把包改道到 B，但新路表项没铺完 -> 第 2 拍 B 无表，黑洞。
INFLIGHT_REPLACE_BLACKHOLE: dict[str, Any] = {
    "drill": {
        "prefix": "198.51.100.0/24",
        "old_path": ["I", "S", "A", "E"],
        "new_path": ["I", "S", "B", "E"],
        "table_capacities": {"I": 1, "S": 1, "A": 1, "B": 1, "E": 1},
        "existing_packets": [{"id": "E01", "switch": "S"}],
        "arrival_ticks": [],
        "buffer_capacity": 1,
        "deadline": 3,
    },
    "script": [{"tick": 1, "action": "replace", "switch": "S"}],
    "cutover_tick": None,
    "closed_ticks": [],
    "replay_ticks": 2,
}

# 7) What-if 删除变体：第 1 拍删掉 S 的旧项下一跳，包在 S 当场黑洞。
INFLIGHT_DELETE_WHATIF: dict[str, Any] = {
    "drill": {
        "prefix": "198.51.100.0/24",
        "old_path": ["I", "S", "A", "E"],
        "new_path": ["I", "S", "B", "E"],
        "table_capacities": {"I": 1, "S": 1, "A": 1, "B": 1, "E": 1},
        "existing_packets": [{"id": "E01", "switch": "S"}],
        "arrival_ticks": [],
        "buffer_capacity": 1,
        "deadline": 3,
    },
    "script": [{"tick": 1, "action": "delete", "switch": "S"}],
    "cutover_tick": None,
    "closed_ticks": [],
    "replay_ticks": 2,
}

PLAN_EXAMPLES: dict[str, dict[str, Any]] = {
    "宽裕双版本_有解": {
        "summary": "有解·双版本共存（推荐先试）",
        "description": "入口容量 2，可同时挂旧/新下一跳；含 1 个存量包和第 2、6 拍到包。",
        "value": FEASIBLE_DUAL,
    },
    "容量1_等包离开再替换_有解": {
        "summary": "有解·表容=1 的等待与原子替换",
        "value": FEASIBLE_REPLACE_WAIT,
    },
    "缓冲上限1_错峰_有解": {
        "summary": "有解·饱和到包下用 1 个缓冲位错峰",
        "value": BUFFER_BOUNDARY_OK,
    },
    "缓冲0_无解_约束归因": {
        "summary": "无解·缓冲上限 0（旧表照用 + 归因）",
        "value": BUFFER_ZERO_INFEASIBLE,
    },
    "期限不足_无解": {
        "summary": "无解·第 12 拍到包赶不上期限",
        "value": DEADLINE_INFEASIBLE,
    },
}

WHATIF_EXAMPLES: dict[str, dict[str, Any]] = {
    "在途包遇replace_改道并偏路径送达": {
        "summary": "在途包当头被 replace：改道到新路，随后被偏路径送达（静默违规）",
        "value": INFLIGHT_WHATIF,
    },
    "在途包遇replace_改道后下一拍黑洞": {
        "summary": "replace 后新路未铺完：包被改道 B，下一拍无表项丢弃",
        "value": INFLIGHT_REPLACE_BLACKHOLE,
    },
    "在途包遇delete_当场黑洞": {
        "summary": "在途包当头被 delete：无表项丢弃",
        "value": INFLIGHT_DELETE_WHATIF,
    },
}

ALL_FIXTURES: dict[str, Any] = {
    "feasible_dual": FEASIBLE_DUAL,
    "feasible_replace_wait": FEASIBLE_REPLACE_WAIT,
    "buffer_boundary_ok": BUFFER_BOUNDARY_OK,
    "buffer_zero_infeasible": BUFFER_ZERO_INFEASIBLE,
    "deadline_infeasible": DEADLINE_INFEASIBLE,
    "inflight_whatif": INFLIGHT_WHATIF,
    "inflight_replace_blackhole": INFLIGHT_REPLACE_BLACKHOLE,
    "inflight_delete_whatif": INFLIGHT_DELETE_WHATIF,
}
