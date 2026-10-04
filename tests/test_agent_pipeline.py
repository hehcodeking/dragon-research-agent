import unittest

from dragon_agent.agents import AgentContext, AgentPipeline, SafetyAgent
from dragon_agent.agents.research import architecture_manifest


class Step:
    def __init__(self, name, fail=False):
        self.name = name
        self.role = f"role-{name}"
        self.inputs = ("value",)
        self.outputs = ("value",)
        self.fail = fail

    def run(self, context):
        if self.fail:
            raise ValueError("secret raw provider detail")
        context.artifacts[self.name] = "done"
        return f"completed {self.name}"


class AgentPipelineTests(unittest.TestCase):
    def test_pipeline_is_ordered_and_exports_safe_trace(self):
        context = AgentPipeline([Step("one"), Step("two")]).run(AgentContext("test", {}))
        self.assertEqual([event.agent for event in context.events], ["one", "two"])
        trace = context.public_trace()
        self.assertEqual(trace["status"], "completed")
        self.assertEqual(trace["artifacts"], {"one": "done", "two": "done"})
        self.assertTrue(all(event["duration_ms"] >= 0 for event in trace["events"]))

    def test_failure_stops_and_does_not_copy_exception_detail(self):
        context = AgentContext("test", {})
        with self.assertRaises(ValueError):
            AgentPipeline([Step("one"), Step("broken", True), Step("never")]).run(context)
        self.assertEqual([event.agent for event in context.events], ["one", "broken"])
        self.assertEqual(context.events[-1].status, "failed")
        self.assertNotIn("secret", context.events[-1].summary)

    def test_duplicate_or_empty_pipeline_is_rejected(self):
        with self.assertRaises(ValueError):
            AgentPipeline([])
        with self.assertRaises(ValueError):
            AgentPipeline([Step("same"), Step("same")])

    def test_architecture_manifest_has_five_specialists_and_no_live_route(self):
        manifest = architecture_manifest()
        self.assertEqual(len(manifest["agents"]), 5)
        self.assertEqual(manifest["agents"][-1]["name"], "artifact-publisher")
        self.assertEqual(manifest["safety"]["live_order_route"], "disabled")

    def test_safety_agent_rejects_execution_capability(self):
        context = AgentContext("test", {}, report={"regime": {"allow_entries": False},
            "candidates": [{"execution_ready": True, "target_weight": 0}]})
        with self.assertRaises(ValueError):
            SafetyAgent().run(context)


if __name__ == "__main__":
    unittest.main()
