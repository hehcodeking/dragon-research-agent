"""Domain agents that compose the existing deterministic research modules."""
from collections import Counter
from datetime import datetime
from pathlib import Path

from .core import AgentContext
from ..data import SHANGHAI, save_json


class DataAgent:
    name, role = "market-data", "读取并标准化市场证据"
    inputs, outputs = ("provider_or_import",), ("frame",)

    def __init__(self, supplier):
        self.supplier = supplier

    def run(self, context):
        frame = self.supplier()
        if not isinstance(frame, dict) or frame.get("schema_version") != 1:
            raise ValueError("Market Data Agent returned an unsupported frame")
        context.frame = frame
        return f"标准化 {len(frame.get('stocks', []))} 个候选；来源 {frame.get('source', 'unknown')}。"


class ResearchAgent:
    name, role = "research-scoring", "执行基础实时评分与时点门禁"
    inputs, outputs = ("frame", "config"), ("report",)

    def run(self, context):
        from ..realtime import recommend_realtime
        context.report = recommend_realtime(context.frame, context.config)
        return f"生成 {len(context.report.get('candidates', []))} 个研究候选；blocked={context.report.get('blocked')}。"


class EvidenceAgent:
    name, role = "v2-evidence", "冻结 9:25/10:00 证据并计算 V2 分数"
    inputs, outputs = ("frame", "report", "assessments"), ("report.model_v2",)

    def __init__(self, tracker, assessment_supplier=None):
        self.tracker = tracker
        self.assessment_supplier = assessment_supplier

    def run(self, context):
        context.assessments = self.assessment_supplier() if self.assessment_supplier else None
        context.report = self.tracker.attach(context.frame, context.report, context.assessments)
        stages = sum(1 for row in context.report.get("candidates", [])
                     for key in ("auction_0925", "intraday_1000")
                     if row.get("model_v2", {}).get(key))
        return f"附加 {stages} 份冻结阶段证据；缺失项保持未计分。"


class SafetyAgent:
    name, role = "safety-guard", "验证研究输出不会成为交易指令"
    inputs, outputs = ("report",), ("safety_invariants",)

    def run(self, context):
        report = context.report
        candidates = report.get("candidates", [])
        if report.get("regime", {}).get("allow_entries") is not False:
            raise ValueError("Realtime research must keep allow_entries false")
        for row in candidates:
            if row.get("execution_ready") is not False or row.get("target_weight") != 0:
                raise ValueError("Realtime candidate attempted to cross the execution boundary")
            stages = row.get("model_v2", {})
            if any(stage and stage.get("vetoes") for stage in
                   (stages.get("auction_0925"), stages.get("intraday_1000"))):
                if row.get("action") != "回避":
                    raise ValueError("V2 veto must downgrade the candidate")
        report["agent_safety"] = {"checked": True, "live_order_route": "disabled",
                                  "candidates_checked": len(candidates),
                                  "invariants": ["allow_entries=false", "execution_ready=false",
                                                 "target_weight=0", "V2否决优先"]}
        return f"验证 {len(candidates)} 个候选；实盘路由保持关闭。"


class PublisherAgent:
    name, role = "artifact-publisher", "原子保存报告并生成可审计界面"
    inputs, outputs = ("frame", "report", "connection"), ("json", "html", "agent_console")

    def __init__(self, out, refresh_seconds=0, connection_supplier=None):
        self.out = Path(out)
        self.refresh_seconds = refresh_seconds
        self.connection_supplier = connection_supplier

    def run(self, context):
        from ..agent_dashboard import write_agent_dashboard
        from ..realtime_dashboard import write_realtime_dashboard
        context.connection = self.connection_supplier() if self.connection_supplier else None
        # Add prior events now; publisher completion is appended by the runtime.
        trace = context.public_trace()
        trace["pipeline"] = [event.agent for event in context.events] + [self.name]
        context.report["agent_trace"] = trace
        save_json(self.out / "frame.json", context.frame)
        save_json(self.out / "report.json", context.report)
        capture = datetime.now(SHANGHAI).strftime("%Y%m%d-%H%M%S-%f") + ".json"
        save_json(self.out / "captures" / capture, context.frame)
        write_realtime_dashboard(self.out / "index.html", context.report,
                                 refresh_seconds=self.refresh_seconds,
                                 connection=context.connection)
        write_agent_dashboard(self.out / "agent.html", context.report, context.connection)
        context.artifacts.update(report=str((self.out / "index.html").resolve()),
                                 agent_console=str((self.out / "agent.html").resolve()),
                                 json=str((self.out / "report.json").resolve()))
        actions = Counter(row.get("action") for row in context.report.get("candidates", []))
        return f"发布工作台与 Agent Console；结论分布 {dict(actions)}。"

    def finalize(self, context):
        """Rewrite the small report surfaces once the publisher event exists."""
        from ..agent_dashboard import write_agent_dashboard
        from ..realtime_dashboard import write_realtime_dashboard
        context.report["agent_trace"] = context.public_trace()
        save_json(self.out / "report.json", context.report)
        write_realtime_dashboard(self.out / "index.html", context.report,
                                 refresh_seconds=self.refresh_seconds,
                                 connection=context.connection)
        write_agent_dashboard(self.out / "agent.html", context.report, context.connection)


def architecture_manifest():
    """Static learning manifest, independent of credentials and providers."""
    classes = (DataAgent, ResearchAgent, EvidenceAgent, SafetyAgent, PublisherAgent)
    return {"schema_version": 1, "name": "dragon-research-pipeline",
            "pattern": "sequential-specialist-agents-with-shared-context",
            "agents": [{"name": cls.name, "role": cls.role,
                        "inputs": list(cls.inputs), "outputs": list(cls.outputs)} for cls in classes],
            "safety": {"live_order_route": "disabled", "failure_mode": "fail_closed",
                       "invariants": ["evidence timestamps required", "missing values stay missing",
                                      "research output cannot submit orders"]}}
