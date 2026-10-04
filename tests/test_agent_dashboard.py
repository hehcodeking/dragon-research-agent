import json
from pathlib import Path
import re
import tempfile
import unittest

from dragon_agent.agent_dashboard import write_agent_dashboard


class AgentDashboardTests(unittest.TestCase):
    def report(self):
        return {"source": "demo", "blocked": False, "session_status": "continuous",
                "summary": {"actions": {"等待确认": 1}},
                "candidates": [{"symbol": "000001.SZ"}],
                "regime": {"allow_entries": False},
                "agent_safety": {"checked": True, "candidates_checked": 1,
                    "invariants": ["allow_entries=false"]},
                "agent_trace": {"run_id": "abc123", "events": [{"agent": "market-data",
                    "role": "data", "status": "completed", "duration_ms": 2,
                    "summary": "done"}], "artifacts": {}}}

    def test_console_contains_navigation_pipeline_and_embedded_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_agent_dashboard(Path(directory) / "agent.html", self.report())
            html = path.read_text()
        self.assertIn("Agent 执行链路", html)
        self.assertIn('id="observe-side"', html)
        self.assertIn("R.source==='demo'?'realtime.html':'index.html'", html)
        self.assertIn("导出运行追踪", html)
        payload = re.search(r'<script id="payload" type="application/json">(.*?)</script>', html, re.S)
        value = json.loads(payload.group(1))
        self.assertEqual(value["report"]["agent_trace"]["run_id"], "abc123")

    def test_script_breakout_is_escaped(self):
        report = self.report()
        attack = "</script><script>alert(1)</script>"
        report["agent_trace"]["events"][0]["summary"] = attack
        with tempfile.TemporaryDirectory() as directory:
            html = write_agent_dashboard(Path(directory) / "agent.html", report).read_text()
        self.assertNotIn(attack, html)
        self.assertIn("\\u003c/script>", html)


if __name__ == "__main__":
    unittest.main()
