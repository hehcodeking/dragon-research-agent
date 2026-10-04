"""Run the report JavaScript against a tiny DOM to verify V2 display semantics."""
import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from dragon_agent.demo import realtime_demo_frame
from dragon_agent.realtime import recommend_realtime
from dragon_agent.realtime_dashboard import write_realtime_dashboard


_BUNDLED_NODE = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
NODE = shutil.which("node") or (str(_BUNDLED_NODE) if _BUNDLED_NODE.is_file() else None)


def report():
    frame = realtime_demo_frame()
    result = recommend_realtime(frame, now=frame["available_at"])
    result["candidates"] = result["candidates"][:1]
    # Keep the compatibility fixture independent of any new default scorer.
    result.pop("model_v2", None)
    result["candidates"][0].pop("model_v2", None)
    return result


def stage(complete=False):
    return {"score": 0, "coverage": 1 if complete else 0, "available_points": 100 if complete else 0,
            "missing_count": 0 if complete else 1, "complete": complete, "grade": "D",
            "status": "complete" if complete else "partial", "status_text": "完整核验" if complete else "尚缺证据",
            "asof": "2026-10-09T09:25:00+08:00",
            "groups": [{"key": "auction", "label": "竞价质量", "score": 0, "max_score": 100,
                        "available_points": 100 if complete else 0}],
            "factors": [{"key": "open_times", "label": "昨日开板次数", "category": "昨日结构",
                         "max_score": 100, "score": 0, "available": complete, "value": 0 if complete else None,
                         "reason": "已有真实观测" if complete else "接口缺少字段", "source": "测试来源",
                         "observed_at": "2026-10-08T15:00:00+08:00", "available_at": "2026-10-08T15:01:00+08:00"}],
            "vetoes": [], "warnings": []}


def with_v2(auction=None, intraday=None, delta=None):
    result = report()
    result["model_v2"] = {"version": "2.0", "note": "V2.0 独立证据评分"}
    result["candidates"][0]["model_v2"] = {"version": "2.0", "auction_0925": auction,
                                            "intraday_1000": intraday, "delta": delta}
    return result


def run_dashboard(research):
    """Execute the actual inline JS, without a browser/network or DOM library."""
    with tempfile.TemporaryDirectory() as directory:
        html_path = Path(directory) / "index.html"
        write_realtime_dashboard(html_path, research)
        html = html_path.read_text()
        payloads = {name: json.loads(re.search(r'<script id="' + name + r'" type="application/json">(.*?)</script>', html, re.S).group(1))
                    for name in ("payload", "connection-payload")}
        script = re.search(r"<script>('use strict';.*?)</script>", html, re.S).group(1)
        prefix = """
const elements = new Map();
function element(id) { if (!elements.has(id)) elements.set(id, {textContent:'', innerHTML:'',
  style:{}, hidden:true, value:id==='filter'?'all':''}); return elements.get(id); }
global.document = {getElementById:element, addEventListener:()=>{}};
global.setInterval = ()=>0;
const initial = __DATA__;
for (const id of ['payload','connection-payload']) element(id).textContent=JSON.stringify(initial[id]);
""".replace("__DATA__", json.dumps(payloads, ensure_ascii=False))
        suffix = """
console.log(JSON.stringify({hidden:element('v2-card').hidden, v2:element('v2-rows').innerHTML,
  detail:element('details').innerHTML, note:element('v2-note').textContent,
  originalRows:element('rows').innerHTML, report:R, connection:element('connection-status').textContent}));
"""
        js_path = Path(directory) / "report-test.js"
        js_path.write_text(prefix + script + suffix)
        process = subprocess.run([NODE, str(js_path)], text=True, capture_output=True, check=True, timeout=10)
    return json.loads(process.stdout)


@unittest.skipUnless(NODE, "Node.js required to execute inline report JavaScript")
class V2DashboardTests(unittest.TestCase):
    def test_missing_v2_preserves_old_report_and_connection_panel(self):
        research = report()
        rendered = run_dashboard(research)
        self.assertTrue(rendered["hidden"])
        self.assertEqual(rendered["v2"], "")
        self.assertNotIn("V2.0 分阶段证据", rendered["detail"])
        self.assertEqual(rendered["report"], research)
        self.assertEqual(rendered["connection"], "最近连接未验证")

    def test_partial_zero_is_explicitly_incomplete_and_future_stage_waits(self):
        rendered = run_dashboard(with_v2(auction=stage()))
        self.assertFalse(rendered["hidden"])
        self.assertIn("0.0 <small>/ 100</small>", rendered["v2"])
        self.assertIn("证据覆盖 0%", rendered["v2"])
        self.assertIn("证据不足", rendered["v2"])
        self.assertIn("等待采集", rendered["v2"])
        self.assertIn("缺证据造成的 0 分不能解释为完整评分低", rendered["detail"])
        self.assertIn("—（缺证据）", rendered["detail"])
        self.assertIn("缺失项目", rendered["detail"])

    def test_complete_zero_and_observed_zero_remain_real_values(self):
        rendered = run_dashboard(with_v2(auction=stage(True)))
        self.assertIn("证据覆盖 100% · 证据完整", rendered["v2"])
        self.assertIn("完整评估得分", rendered["v2"])
        self.assertIn('<td class="num">0</td>', rendered["detail"])
        self.assertNotIn("—（缺证据）", rendered["detail"])
        self.assertNotIn("缺失项目", rendered["detail"])

    def test_zero_delta_displays_but_incomparable_numeric_delta_does_not(self):
        research = with_v2(stage(True), stage(True), {"value": 0, "label": "保持", "comparable": True, "reason": "同口径"})
        self.assertIn("0.0 分", run_dashboard(research)["v2"])
        research["candidates"][0]["model_v2"]["delta"].update(value=15, comparable=False, reason="缺失项目不同")
        rendered = run_dashboard(research)
        self.assertIn("不可比较", rendered["v2"])
        self.assertIn("缺失项目不同", rendered["v2"])
        self.assertNotIn("15.0 分", rendered["v2"])

    def test_vetoes_provenance_and_untrusted_text_are_displayed_safely(self):
        auction = stage(True)
        attack = '</script><img src=x onerror="alert(1)">'
        auction.update(status="vetoed", vetoes=[attack], warnings=[attack])
        auction["groups"][0]["label"] = attack
        auction["factors"][0].update(source=attack, label=attack, reason=attack)
        research = with_v2(auction=auction)
        research["model_v2"]["note"] = attack
        rendered = run_dashboard(research)
        self.assertIn("触发否决项", rendered["v2"])
        self.assertIn("否决项", rendered["detail"])
        self.assertIn("观测 2026-10-08T15:00:00+08:00", rendered["detail"])
        self.assertIn("可用 2026-10-08T15:01:00+08:00", rendered["detail"])
        self.assertNotIn(attack, rendered["detail"])
        self.assertIn("&lt;/script&gt;&lt;img", rendered["detail"])
        # Metadata uses textContent, so the original string is safe plain text.
        self.assertEqual(rendered["note"], attack)

    def test_v2_rendering_does_not_change_existing_research_gates(self):
        research = with_v2(stage(True), stage(True), {"value": 0, "comparable": True})
        research["candidates"][0].update(action="等待确认", recommendation_eligible=False, execution_ready=False)
        research["blocked"] = True
        original = copy.deepcopy(research)
        rendered = run_dashboard(research)
        self.assertEqual(rendered["report"], original)
        self.assertIn("等待确认", rendered["originalRows"])


if __name__ == "__main__":
    unittest.main()
