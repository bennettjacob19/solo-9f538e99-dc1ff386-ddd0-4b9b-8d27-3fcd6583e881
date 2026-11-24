"""请求 / 响应的 Pydantic 模型（中文字段说明会直接显示在 /docs）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from .domain import MAX_TICKS, MAX_SWITCHES


class PacketPositionIn(BaseModel):
    id: str | None = Field(
        default=None, description="探测包编号，可不填，缺省为 E01、E02…"
    )
    switch: str = Field(..., description="该存量探测包当前所在交换机（必须在旧路序列中）")

    model_config = {"json_schema_extra": {"example": {"id": "E01", "switch": "S1"}}}


class DrillRequest(BaseModel):
    prefix: str = Field(default="203.0.113.0/24", description="要更换路径的业务前缀（仅展示/记录用）")
    old_path: list[str] = Field(
        ..., min_length=2,
        description="旧路：从入口到出口的无环交换机序列，例如 [I,S1,S2,E]，初始只安装这条路的表项",
    )
    new_path: list[str] = Field(
        ..., min_length=2,
        description="新路：与旧路同起点、同终点的无环交换机序列；两路不同交换机合计 ≤ 6 台",
    )
    table_capacities: dict[str, int] | int = Field(
        ...,
        description=(
            "每台交换机对该前缀可占的表项上限，如 {\"I\":2,\"S1\":1}；"
            "也可直接填一个整数，表示两路所有交换机统一使用该上限。"
            "两条版本下一跳不同时各占 1 项，相同则共享 1 项。"
        ),
    )
    existing_packets: list[PacketPositionIn | str] = Field(
        default_factory=list,
        description="旧路上已有探测包的位置；可填 {\"id\":\"E01\",\"switch\":\"S1\"}，也可直接填交换机名字符串 \"S1\"",
    )
    arrival_ticks: list[int | dict[str, Any]] = Field(
        default_factory=list,
        description=f"后续探测包到达拍点（按拍，1..{MAX_TICKS}），每拍最多 1 个；可填 [2,6] 或 [{{\"tick\":2}}]",
    )
    buffer_capacity: int = Field(
        ..., ge=0,
        description="入口缓冲上限（包数）；入口关闭时到包暂存，暂存数不得超过它",
    )
    deadline: int = Field(
        ..., ge=1, le=MAX_TICKS,
        description=f"完成期限（第几拍结束前）：须送达全部包、入口已切新路、旧路专用表项撤净；≤{MAX_TICKS}",
    )
    release_per_tick: int | None = Field(
        default=None, ge=1,
        description="高级可选项：入口开时每拍最多放行几个包；缺省 null 表示一次放行全部已到包",
    )

    @field_validator("table_capacities", mode="before")
    @classmethod
    def _coerce_scalar_capacity(cls, v: Any, info: ValidationInfo) -> Any:
        # 允许直接给一个全局整数上限
        if isinstance(v, int) and not isinstance(v, bool):
            switches = set(info.data.get("old_path") or []) | set(info.data.get("new_path") or [])
            return {s: int(v) for s in switches}
        return v

    @field_validator("existing_packets", mode="before")
    @classmethod
    def _coerce_positions(cls, v: Any) -> Any:
        if not isinstance(v, list):
            return v
        out: list[Any] = []
        for item in v:
            if isinstance(item, str):
                out.append({"switch": item})
            elif isinstance(item, dict):
                sw = item.get("switch") or item.get("position") or item.get("at") or item.get("node")
                norm = dict(item)
                norm["switch"] = sw
                out.append(norm)
            else:
                out.append(item)
        return out

    @field_validator("arrival_ticks", mode="before")
    @classmethod
    def _coerce_arrival_objects(cls, v: Any) -> Any:
        if not isinstance(v, list):
            return v
        out: list[Any] = []
        for item in v:
            if isinstance(item, dict):
                out.append(item.get("tick", item.get("at", item.get("arrival_tick", item.get("time")))))
            else:
                out.append(item)
        return out

    @field_validator("arrival_ticks")
    @classmethod
    def _check_tick_range(cls, v: list[int]) -> list[int]:
        for t in v:
            if not isinstance(t, int) or isinstance(t, bool):
                raise ValueError(f"到达拍点 {t!r} 不是 1..{MAX_TICKS} 的整数拍")
            if not (1 <= t <= MAX_TICKS):
                raise ValueError(f"到达拍点 {t} 超出 1..{MAX_TICKS}")
        return v  # type: ignore[return-value]

    @model_validator(mode="after")
    def _check_shared_limits(self) -> "DrillRequest":
        if len(set(self.old_path) | set(self.new_path)) > MAX_SWITCHES:
            raise ValueError(
                f"两路合计涉及 {len(set(self.old_path) | set(self.new_path))} 台交换机，"
                f"超过上限 {MAX_SWITCHES} 台"
            )
        if len(set(self.arrival_ticks)) != len(self.arrival_ticks):
            raise ValueError("arrival_ticks 中存在重复拍点：每拍最多到 1 个包")
        return self


class ScriptedTableAction(BaseModel):
    tick: int = Field(..., ge=1, le=MAX_TICKS, description="在第几拍表项阶段执行")
    action: Literal["add", "delete", "replace"]
    switch: str
    nexthop: str | None = Field(
        default=None,
        description="add 的下一跳 / replace 的新下一跳；不填则按新路（add、replace-new）推导",
    )
    old_nexthop: str | None = Field(
        default=None, description="replace 的旧下一跳；不填则按旧路推导"
    )


class WhatIfRequest(BaseModel):
    drill: DrillRequest = Field(..., description="与正式演练相同的拓扑/容量/包/期限输入")
    script: list[ScriptedTableAction] = Field(
        default_factory=list,
        description="故意安排的表项动作脚本（每拍至多一条），允许在在途包所在交换机上强改/强删",
    )
    cutover_tick: int | None = Field(
        default=None, ge=1, le=MAX_TICKS,
        description="入口切新路的拍点；null 表示始终留在旧平面（用于纯在途碰撞演示）",
    )
    closed_ticks: list[int] = Field(default_factory=list, description="入口关闭的拍点集合")
    replay_ticks: int | None = Field(
        default=None, ge=1, le=MAX_TICKS, description="复放多少拍，缺省用 drill.deadline"
    )


class StepBody(BaseModel):
    note: str | None = Field(default=None, description="可选备注，不影响执行")


class PlanAck(BaseModel):
    status: str
    summary_zh: str
    session_id: str | None = None
    goal_tick: int | None = None
    search_nodes: int | None = None
    elapsed_ms: float | None = None
    plan_overview: list[dict[str, Any]] = Field(default_factory=list)
    diagnosis: dict[str, Any] | None = None
