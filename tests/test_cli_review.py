"""Offline CLI checks: synthetic mode remains explicit; failures cannot fake data."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dragon_agent.cli import main
from dragon_agent.data import CredentialError, DataError
from dragon_agent.demo import realtime_demo_frame


class CliReviewTests(unittest.TestCase):
    def call(self, args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(args)
        return code, json.loads(out.getvalue().strip())

    def test_demo_is_explicitly_synthetic_and_entirely_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("dragon_agent.cli.HithinkClient", side_effect=AssertionError("Unexpected network client")) as client:
                code, result = self.call(["demo", "--out", directory])
                client.assert_not_called()
            self.assertEqual(code, 0)
            self.assertIs(result["synthetic_only"], True)
            for name in ("report.json", "realtime-report.json"):
                report = json.loads((Path(directory) / name).read_text())
                self.assertEqual(report["source"], "demo")
            self.assertTrue((Path(directory) / "index.html").is_file())
            self.assertTrue((Path(directory) / "realtime.html").is_file())
            self.assertTrue((Path(directory) / "agent.html").is_file())
            self.assertIn("agent_console", result)

    def test_missing_credentials_never_create_a_demo_fallback(self):
        for command in ("collect", "realtime"):
            with tempfile.TemporaryDirectory() as directory:
                with patch("dragon_agent.cli.HithinkClient", side_effect=CredentialError("No configured credential")):
                    code, result = self.call([command, "--out", directory])
                self.assertEqual(code, 2)
                self.assertFalse(result["ok"])
                self.assertFalse(result["live_orders_sent"])
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_collection_failure_does_not_replace_real_data_with_demo(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("dragon_agent.cli.HithinkClient", return_value=object()):
                with patch("dragon_agent.realtime_data.RealtimeCollector.collect", side_effect=DataError("Upstream unavailable")):
                    code, result = self.call(["realtime", "--out", directory])
            self.assertEqual(code, 2)
            self.assertEqual(result["error"], "Upstream unavailable")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_imported_stale_snapshot_uses_current_clock_and_keeps_source(self):
        sample = realtime_demo_frame()
        sample["source"] = "imported"
        sample["trading_date"] = "2000-01-03"
        sample["previous_trading_date"] = "1999-12-31"
        sample["asof"] = sample["available_at"] = "2000-01-03T09:40:00+08:00"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            source.write_text(json.dumps(sample), encoding="utf-8")
            output = Path(directory) / "output"
            with patch("dragon_agent.cli.HithinkClient", side_effect=AssertionError("Unexpected network client")) as client:
                code, result = self.call(["realtime", "--input", str(source), "--out", str(output)])
                client.assert_not_called()
            self.assertEqual(code, 0)
            self.assertTrue(result["blocked"])
            self.assertEqual(result["source"], "imported")
            report = json.loads((output / "report.json").read_text())
            self.assertNotEqual(report["decision_time"], sample["asof"])
            self.assertTrue(all(row["action"] != "重点观察" for row in report["candidates"]))
            self.assertTrue(all(row["execution_ready"] is False for row in report["candidates"]))

    def test_import_cannot_masquerade_as_refreshing_live_source(self):
        with tempfile.TemporaryDirectory() as directory:
            code, result = self.call(["realtime", "--input", "unused.json", "--watch", "--out", directory])
            self.assertEqual(code, 2)
            self.assertIn("导入文件", result["error"])
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
