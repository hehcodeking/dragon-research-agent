"""最小扩展示例：向上下文添加公开、非敏感的质量摘要。"""
from dragon_agent.agents import AgentContext, AgentPipeline


class CoverageNoteAgent:
    name = "coverage-note"
    role = "汇总候选证据覆盖率"
    inputs = ("report",)
    outputs = ("report.coverage_note",)

    def run(self, context):
        candidates = context.report.get("candidates", [])
        values = [row.get("confidence") for row in candidates]
        values = [value for value in values if isinstance(value, (int, float))]
        average = sum(values) / len(values) if values else 0
        context.report["coverage_note"] = {"average": average, "samples": len(values)}
        return f"汇总 {len(values)} 个候选的证据覆盖率。"


if __name__ == "__main__":
    # 实际使用时把此 Agent 插入 ResearchAgent 之后、SafetyAgent 之前。
    pipeline = AgentPipeline([CoverageNoteAgent()])
    context = AgentContext("example", {}, report={"candidates": [{"confidence": 0.8}]})
    result = pipeline.run(context)
    print(result.public_trace())
