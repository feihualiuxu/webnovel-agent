from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

from .autopilot import AutoOptions, AutoPilot
from .llm_client import LLMClient


class GraphState(TypedDict, total=False):
    phase: str
    message: str


def run_langgraph(project: Path, llm: LLMClient, options: AutoOptions) -> str:
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise RuntimeError("本地尚未安装 LangGraph。请先运行 install_tools.bat，或使用 --engine builtin。") from exc

    pilot = AutoPilot(project, llm, options)

    def load_state(_: GraphState) -> GraphState:
        awaiting = pilot.state.get("awaiting") or {}
        if awaiting and options.stop_at_checkpoints and awaiting.get("type") != "source_failures":
            return {"phase": "awaiting"}
        phase = pilot.state.get("phase", "new")
        if phase == "completed" and pilot.resume_completed_if_target_extended():
            phase = pilot.state.get("phase", "writing")
        return {"phase": phase}

    def decompose(_: GraphState) -> GraphState:
        try:
            pilot.decompose_sources()
        except RuntimeError:
            awaiting = pilot.state.get("awaiting") or {}
            if awaiting.get("type") == "source_failures":
                return {"phase": "awaiting", "message": pilot._awaiting_message(awaiting)}
            raise
        return {"phase": pilot.state.get("phase", "decomposed")}

    def blueprint(_: GraphState) -> GraphState:
        pilot.build_blueprint()
        return {"phase": pilot.state.get("phase", "blueprint_pending")}

    def write(_: GraphState) -> GraphState:
        pilot.write_until_limit()
        return {"phase": pilot.state.get("phase", "writing")}

    def wait(_: GraphState) -> GraphState:
        awaiting = pilot.state.get("awaiting") or {"message": "等待确认。"}
        return {"phase": "awaiting", "message": pilot._awaiting_message(awaiting)}

    def done(_: GraphState) -> GraphState:
        return {"phase": pilot.state.get("phase", "completed"), "message": "自动工作流已到达本次目标章节。"}

    def route(state: GraphState) -> str:
        if pilot.state.get("awaiting") and options.stop_at_checkpoints:
            return "wait"
        phase = state.get("phase")
        if phase == "awaiting":
            return "wait"
        if phase == "new":
            return "decompose"
        if phase == "decomposed":
            return "blueprint"
        if phase == "outline_pending":
            return "wait"
        if phase == "blueprint_pending":
            return "wait"
        if phase == "writing":
            return "write"
        if phase == "completed":
            return "done"
        return "done"

    graph = StateGraph(GraphState)
    graph.add_node("load", load_state)
    graph.add_node("decompose", decompose)
    graph.add_node("blueprint", blueprint)
    graph.add_node("write", write)
    graph.add_node("wait", wait)
    graph.add_node("done", done)
    graph.add_edge(START, "load")
    graph.add_conditional_edges(
        "load",
        route,
        {
            "decompose": "decompose",
            "blueprint": "blueprint",
            "write": "write",
            "wait": "wait",
            "done": "done",
        },
    )
    graph.add_conditional_edges(
        "decompose",
        route,
        {
            "blueprint": "blueprint",
            "wait": "wait",
            "done": "done",
        },
    )
    graph.add_conditional_edges(
        "blueprint",
        route,
        {
            "write": "write",
            "wait": "wait",
            "done": "done",
        },
    )
    graph.add_conditional_edges(
        "write",
        route,
        {
            "write": "write",
            "wait": "wait",
            "done": "done",
        },
    )
    graph.add_edge("wait", END)
    graph.add_edge("done", END)

    app = graph.compile()
    result: dict[str, Any] = app.invoke({})
    if result.get("message"):
        return str(result["message"])
    if pilot.state.get("awaiting"):
        return pilot._awaiting_message(pilot.state["awaiting"])
    return f"LangGraph 自动流完成一步，当前阶段：{pilot.state.get('phase')}"
