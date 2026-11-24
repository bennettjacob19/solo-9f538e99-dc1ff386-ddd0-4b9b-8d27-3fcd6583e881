"""内存会话存储：支持在 /docs 里 create -> step -> step 逐拍执行。

本地演练工具，单值班员使用；进程重启会话即消失，这是已知缺项（见 README）。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from .domain import Static
from .planner import FindResult, diagnose, find_earliest_plan
from .runner import Runner
from .service import infeasible_report, plan_overview


@dataclass
class Session:
    session_id: str
    request_dict: dict[str, Any]
    stc: Static
    result: FindResult
    status: str  # feasible | infeasible
    runner: Runner | None = None
    diagnosis: dict[str, Any] | None = None
    error_report: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def finished(self) -> bool:
        if self.runner is None or self.result.actions is None:
            return True
        return self.runner.tick >= len(self.result.actions)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._global = threading.Lock()

    def create(self, request_dict: dict[str, Any], stc: Static) -> Session:
        result = find_earliest_plan(stc)
        sid = uuid.uuid4().hex[:12]
        if result.feasible and result.actions is not None:
            sess = Session(
                session_id=sid, request_dict=request_dict, stc=stc,
                result=result, status="feasible", runner=Runner(stc),
            )
        else:
            diag = diagnose(stc)
            report = infeasible_report(stc, result, diag)
            sess = Session(
                session_id=sid, request_dict=request_dict, stc=stc,
                result=result, status="infeasible", diagnosis=diag,
                error_report=report,
            )
        with self._global:
            self._sessions[sid] = sess
        return sess

    def get(self, sid: str) -> Session | None:
        return self._sessions.get(sid)

    def list_ids(self) -> list[str]:
        return list(self._sessions)

    def step(self, sess: Session) -> dict[str, Any]:
        with sess.lock:
            if sess.status != "feasible" or sess.runner is None or sess.result.actions is None:
                return {"conflict": True, "detail": "该会话无解，旧表照用，不能逐拍执行"}
            if sess.finished:
                return {"conflict": True, "detail": "方案已执行完，可 GET 会话查看完整报告"}
            action = sess.result.actions[sess.runner.tick]
            report = sess.runner.step(action)
            return {"conflict": False, "tick_report": report,
                    "goal_reached": report["goal_reached"],
                    "finished": sess.finished}

    def run_remaining(self, sess: Session) -> None:
        with sess.lock:
            if sess.status == "feasible" and sess.runner is not None and sess.result.actions:
                while sess.runner.tick < len(sess.result.actions):
                    sess.runner.step(sess.result.actions[sess.runner.tick])

    def detail(self, sess: Session) -> dict[str, Any]:
        if sess.status == "infeasible":
            assert sess.error_report is not None
            return {
                "session_id": sess.session_id,
                "status": "infeasible",
                "feasible": False,
                "finished": True,
                "config": sess.request_dict,
                "summary_zh": sess.error_report["summary_zh"],
                "diagnosis": sess.diagnosis,
                "initial_tables": sess.error_report["initial_tables"],
                "old_tables_kept": True,
                "search_nodes": sess.result.nodes,
                "elapsed_ms": round(sess.result.elapsed_ms, 2),
            }
        assert sess.runner is not None and sess.result.actions is not None
        return {
            "session_id": sess.session_id,
            "status": "feasible",
            "feasible": True,
            "finished": sess.finished,
            "config": sess.request_dict,
            "executed_ticks": sess.runner.tick,
            "total_planned_ticks": len(sess.result.actions),
            "goal_tick": len(sess.result.actions),
            "search_nodes": sess.result.nodes,
            "elapsed_ms": round(sess.result.elapsed_ms, 2),
            "plan_overview": plan_overview(sess.stc, sess.result.actions),
            "ticks": sess.runner.tick_reports,
            "packet_trajectories": sess.runner.trajectories(),
            **sess.runner.summary(),
        }


store = SessionStore()
