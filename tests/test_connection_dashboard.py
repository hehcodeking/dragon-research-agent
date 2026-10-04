"""Connection display stays separate from research eligibility and secrets."""
import copy
from contextlib import redirect_stdout
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dragon_agent.cli import main, read_connection_record
from dragon_agent.demo import realtime_demo_frame
from dragon_agent.realtime import recommend_realtime
from dragon_agent.realtime_dashboard import connection_summary, write_realtime_dashboard


NOW = datetime(2026, 10, 4, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
SECRET = "private-test-key-never-show-connection"


def connection(checked=NOW):
    return {"source": "hithink", "status": "connected", "checked_at": checked.isoformat(),
            "auth_verified": True, "snapshot_valid": True, "symbol": "600519.SH", "price": 1258.62,
            "quote_asof": (checked - timedelta(seconds=2)).isoformat(), "freshness": "unverified"}


def report():
    frame = realtime_demo_frame()
    result = recommend_realtime(frame, now=frame["available_at"])
    result.update(source="hithink", blocked=True, blockers=["当前非交易日"], session_status="closed")
    for row in result["candidates"]:
        row.update(action="等待确认", recommendation_eligible=False)
    return result


def payload(html, name):
    match = re.search(r'<script id="' + name + r'" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(match.group(1))


class ConnectionDashboardTests(unittest.TestCase):
    def test_recent_auth_and_snapshot_evidence_is_displayed_separately(self):
        summary = connection_summary(connection(), now=NOW)
        self.assertEqual(summary["state"], "passed")
        self.assertEqual(summary["label"], "最近连接验证通过")
        self.assertEqual(summary["record_freshness"], "recent")
        self.assertEqual(summary["symbol"], "600519.SH")
        self.assertEqual(summary["price"], 1258.62)
        self.assertEqual(summary["freshness"], "unverified")
        self.assertNotIn("当前实时", json.dumps(summary, ensure_ascii=False))

    def test_old_success_is_a_history_record_not_a_current_connection_claim(self):
        summary = connection_summary(connection(NOW - timedelta(days=1)), now=NOW)
        self.assertEqual(summary["state"], "passed")
        self.assertEqual(summary["record_freshness"], "expired")
        self.assertIn("历史验证记录", summary["record_note"])
        self.assertIn("重新验证", summary["record_note"])

    def test_future_validation_or_quote_time_cannot_claim_passed(self):
        future_check = connection(NOW + timedelta(seconds=1))
        future_quote = connection()
        future_quote["quote_asof"] = (NOW + timedelta(seconds=1)).isoformat()
        quote_after_check = connection(NOW - timedelta(seconds=10))
        quote_after_check["quote_asof"] = (NOW - timedelta(seconds=5)).isoformat()
        for value in (future_check, future_quote, quote_after_check):
            with self.subTest(value=value):
                summary = connection_summary(value, now=NOW)
                self.assertEqual(summary["state"], "unverified")
                self.assertEqual(summary["record_freshness"], "invalid")
                self.assertNotEqual(summary["label"], "最近连接验证通过")

    def test_failed_verification_uses_safe_fixed_text(self):
        value = connection()
        value.update(status="auth_failed", auth_verified=False, snapshot_valid=False, error=SECRET, next_step=SECRET)
        summary = connection_summary(value, now=NOW)
        self.assertEqual(summary["state"], "failed")
        self.assertEqual(summary["label"], "最近连接验证失败")
        self.assertNotIn(SECRET, json.dumps(summary))

    def test_missing_unknown_or_incomplete_evidence_remains_unverified(self):
        for value in (None, [], {}, dict(connection(), source="demo"), dict(connection(), auth_verified="true"),
                      dict(connection(), snapshot_valid=1), dict(connection(), checked_at="2026-10-04T08:00:00"),
                      dict(connection(), quote_asof=None), dict(connection(), symbol=None)):
            with self.subTest(value=value):
                self.assertEqual(connection_summary(value, now=NOW)["state"], "unverified")

    def test_nonfinite_boolean_and_unreasonable_price_is_not_rendered(self):
        for price in (True, "1258.62", -1, 0, float("nan"), float("inf"), 10**1000):
            value = connection()
            value["price"] = price
            summary = connection_summary(value, now=NOW)
            self.assertIsNone(summary["price"])
            self.assertEqual(summary["state"], "unverified")
            json.dumps(summary, allow_nan=False)

    def test_html_contains_only_allowlisted_connection_evidence(self):
        value = connection()
        value.update(api_key=SECRET, raw_error=SECRET, issues=[SECRET], next_step=SECRET,
                     provenance={"headers": {"X-api-key": SECRET}}, malicious="</script><script>" + SECRET)
        before, research = copy.deepcopy(value), report()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.html"
            write_realtime_dashboard(path, research, connection=value)
            html = path.read_text()
        self.assertNotIn(SECRET, html)
        self.assertNotIn("fetch(", html)
        self.assertNotIn("XMLHttpRequest", html)
        self.assertIn("报价批次时间不等于逐股成交时间", html)
        self.assertEqual(payload(html, "payload"), research)
        self.assertTrue(payload(html, "payload")["blocked"])
        self.assertEqual(value, before)

    def test_even_whitelisted_string_fields_are_value_validated(self):
        value = connection()
        for field in ("source", "status", "checked_at", "symbol", "price", "quote_asof", "freshness"):
            candidate = dict(value, **{field: SECRET})
            summary = connection_summary(candidate, now=NOW)
            self.assertNotIn(SECRET, json.dumps(summary))

    def test_reader_is_bounded_and_never_surfaces_parse_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connection.json"
            self.assertIsNone(read_connection_record(path))
            for raw in ("not-json-" + SECRET, json.dumps([SECRET]), "x" * 65537, '{"price": NaN}', b"\xff"):
                if isinstance(raw, bytes):
                    path.write_bytes(raw)
                else:
                    path.write_text(raw)
                self.assertIsNone(read_connection_record(path))
            path.write_text(json.dumps(dict(connection(), raw_error=SECRET)))
            selected = read_connection_record(path)
            self.assertNotIn("raw_error", selected)
            self.assertNotIn(SECRET, json.dumps(selected))

    def test_cli_reads_same_output_connection_without_affecting_blocked_report(self):
        sample = realtime_demo_frame()
        sample["source"] = "hithink"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            evidence = connection(datetime.now(ZoneInfo("Asia/Shanghai")))
            (path / "connection.json").write_text(json.dumps(dict(evidence, secret=SECRET)))
            output = io.StringIO()
            with patch("dragon_agent.cli.HithinkClient", return_value=object()), \
                 patch("dragon_agent.realtime_data.RealtimeCollector.collect", return_value=sample), redirect_stdout(output):
                code = main(["realtime", "--out", directory])
            self.assertEqual(code, 0)
            result = json.loads(output.getvalue())
            self.assertTrue(result["blocked"])
            html = (path / "index.html").read_text()
            self.assertEqual(payload(html, "connection-payload")["state"], "passed")
            self.assertTrue(payload(html, "payload")["blocked"])
            self.assertNotIn(SECRET, html)
            self.assertEqual(json.loads((path / "connection.json").read_text())["secret"], SECRET)


if __name__ == "__main__":
    unittest.main()
