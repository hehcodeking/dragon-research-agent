import unittest
from dragon_agent.demo import demo_snapshots
from dragon_agent.strategy import analyze


class TemporalRegressionTests(unittest.TestCase):
    def test_new_quote_batch_cannot_refresh_old_market_pools(self):
        frame = demo_snapshots()[0]
        frame["provenance"]["source_timestamps"] = {"limit-up-pool": ["2026-09-14T09:35:00+08:00"]}
        report = analyze(frame, now=frame["asof"])
        self.assertTrue(report["blocked"])
        self.assertTrue(any("股票池已过期" in text for text in report["blockers"]))
        self.assertFalse(any(row["target_weight"] > 0 for row in report["candidates"]))

    def test_future_pool_time_not_accepted_in_historical_mode(self):
        frame = demo_snapshots()[0]
        frame["provenance"]["source_timestamps"] = {"limit-down-pool": ["2026-09-14T15:01:00+08:00"]}
        report = analyze(frame, now=frame["asof"], historical=True)
        self.assertTrue(report["blocked"])
        self.assertTrue(any("尚未可用" in text for text in report["blockers"]))


if __name__ == "__main__":
    unittest.main()
