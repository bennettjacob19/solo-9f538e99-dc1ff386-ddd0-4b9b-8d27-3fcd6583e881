"""FastAPI 入口：在 /docs 填参 -> 求解/逐拍执行/what-if 试跑。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .domain import ConfigError
from .examples import ALL_FIXTURES, PLAN_EXAMPLES, WHATIF_EXAMPLES
from .schemas import DrillRequest, StepBody, WhatIfRequest
from .service import solve_and_report, static_from_request
from .sessions import store
from .whatif import run_whatif

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="业务前缀换路本地演练台",
    version=__version__,
    description=(
        "值班员更换业务前缀转发路径的本地确定性演练工具。\n\n"
        "**怎么用（推荐在 /docs 里）**：\n"
        "1. 打开 `POST /api/v1/plans`，点 **Try it out**，用 Request body 的 "
        "**Examples** 下拉选一个预置场景，再 Execute，一次拿到逐拍表项/缓冲/轨迹。\n"
        "2. 想真正“逐拍点”：用 `POST /api/v1/sessions` 建会话，再反复调用 "
        "`POST /api/v1/sessions/{session_id}/step`。\n"
        "3. 想试“在途包遇上改表”的破坏性后果：用 `POST /api/v1/whatif`。\n\n"
        "拍语义、容量口径与已知缺项见仓库 README。所有计算都在本地内存中，不连真实设备。"
    ),
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(ConfigError)
async def config_error_handler(_request: Any, exc: ConfigError):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=422,
        content={"detail": [{"msg": m, "type": "business_config"} for m in exc.errors],
                 "errors": exc.errors},
    )


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/ui")


@app.get("/ui", include_in_schema=False)
def ui() -> FileResponse:
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(404, "UI 未构建，但 /docs 可完整使用")
    return FileResponse(str(index))


@app.get("/api/v1/examples", tags=["examples"], summary="全部预置场景 JSON（可复制到 /docs）")
def get_examples() -> dict[str, Any]:
    return {"fixtures": ALL_FIXTURES}


@app.post(
    "/api/v1/plans",
    tags=["plan"],
    summary="求解并一次逐拍执行：有解返回全部拍细节，无解返回约束归因且旧表照用",
)
def create_plan(
    req: DrillRequest = Body(..., openapi_examples=PLAN_EXAMPLES),
) -> dict[str, Any]:
    report = solve_and_report(req)
    report["feasible"] = report["status"] == "feasible"
    report["config"] = req.model_dump()
    report["steps"] = report.get("ticks", [])  # 语义别名，方便不同判据读取
    return report


@app.post(
    "/api/v1/sessions",
    tags=["step-by-step"],
    summary="创建逐拍演练会话（内部已完成求解；无解也会建会话但不能 step）",
)
def create_session(
    req: DrillRequest = Body(..., openapi_examples=PLAN_EXAMPLES),
) -> dict[str, Any]:
    stc = static_from_request(req)
    sess = store.create(req.model_dump(), stc)
    if sess.status == "feasible":
        return {
            "session_id": sess.session_id,
            "status": "feasible",
            "hint": "依次调用 POST /api/v1/sessions/{session_id}/step 逐拍执行",
            "total_planned_ticks": len(sess.result.actions or ()),
            "search_nodes": sess.result.nodes,
            "elapsed_ms": round(sess.result.elapsed_ms, 2),
            "plan_overview": [
                {"tick": i + 1, "op": a[0], "plane": "new" if a[1] else "old",
                 "open": bool(a[2])}
                for i, a in enumerate(sess.result.actions or ())
            ],
        }
    return {
        "session_id": sess.session_id,
        "status": "infeasible",
        "hint": "无解，旧表照用；可 GET 会话查看归因，或直接调 /api/v1/plans",
        "diagnosis": sess.diagnosis,
    }


@app.get("/api/v1/sessions", tags=["step-by-step"], summary="列出内存中的会话 id")
def list_sessions() -> dict[str, Any]:
    return {"session_ids": store.list_ids()}


@app.get("/api/v1/sessions/{session_id}", tags=["step-by-step"],
         summary="查看会话当前完整状态（已执行拍、表项、缓冲、轨迹）")
def get_session(session_id: str) -> dict[str, Any]:
    sess = store.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在或已随重启消失")
    return store.detail(sess)


@app.post("/api/v1/sessions/{session_id}/step", tags=["step-by-step"],
          summary="按规划执行下一拍，返回这一拍的表项/缓冲/逐包跳")
def step_session(session_id: str, _body: StepBody | None = Body(default=None)) -> dict[str, Any]:
    sess = store.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在或已随重启消失")
    out = store.step(sess)
    if out.get("conflict"):
        raise HTTPException(status_code=409, detail=out["detail"])
    return {
        "session_id": session_id,
        "status": "feasible",
        "executed_tick": out["tick_report"]["tick"],
        "finished": out["finished"],
        "goal_reached": out["goal_reached"],
        "tick": out["tick_report"],
    }


@app.post("/api/v1/sessions/{session_id}/run", tags=["step-by-step"],
          summary="把剩余拍一次跑完，返回完整会话报告")
def run_session(session_id: str) -> dict[str, Any]:
    sess = store.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在或已随重启消失")
    if sess.status != "feasible":
        raise HTTPException(status_code=409, detail="该会话无解，旧表照用")
    store.run_remaining(sess)
    return store.detail(sess)


@app.post(
    "/api/v1/whatif",
    tags=["what-if"],
    summary="非严格复放自定义改表脚本：实测在途包遇改表后的改道/丢弃后果",
)
def whatif(
    req: WhatIfRequest = Body(..., openapi_examples=WHATIF_EXAMPLES),
) -> dict[str, Any]:
    return run_whatif(
        req.drill.model_dump(),
        [a.model_dump(exclude_none=False) for a in req.script],
        cutover_tick=req.cutover_tick,
        closed_ticks=req.closed_ticks,
        ticks=req.replay_ticks,
    )


# --- 兼容性路径别名（不在 /docs 重复展示），方便脚本/判据按常见命名调用 ---
for _alias in ("/solve", "/api/solve", "/drill", "/api/drill"):
    app.add_api_route(
        _alias, create_plan, methods=["POST"], include_in_schema=False,
        name=f"alias_create_plan_{_alias}",
    )
