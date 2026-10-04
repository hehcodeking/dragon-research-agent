"""Semantic and temporal regression coverage for non-executable watchlists."""
import copy
from datetime import datetime, timezone
import json
import unittest

from dragon_agent.realtime import recommend_realtime


def frame(clock="10:30:00", phase="continuous"):
    stamp = f"2026-09-30T{clock}+08:00"
    return {
        "schema_version": 1, "source": "demo", "trading_date": "2026-09-30",
        "previous_trading_date": "2026-09-29", "previous_available_at": stamp,
        "asof": stamp, "available_at": stamp, "market_asof": stamp,
        "is_trading_day": True, "session_status": phase,
        "coverage": {"complete": True, "issues": []},
        "stocks": [{
            "symbol": "600000.SH", "name": "合成样本", "is_st": False, "is_new": False,
            "price": 103.0, "prev_close": 100.0, "current_volume": 7_500_000.0,
            "current_turnover": 772_500_000.0, "current_turnover_pct": 5.0,
            "quote_asof": stamp, "batch_quote_asof": stamp,
            "auction": {
                "auction_pct": 2.0, "auction_amount": 30_600_000.0,
                "auction_volume": 300_000.0, "auction_turnover_pct": .5,
                "auction_volume_ratio": 2.0, "auction_yesterday_ratio_pct": 1.0,
                "auction_unmatched": 1000, "data_status": "ready", "auction_phase": "final",
                "asof": "2026-09-30T09:25:00+08:00",
            },
            "previous": {"date": "2026-09-29", "board_count": 3, "open_times": 0,
                         "limit_up_time": "09:40", "final_seal_time": "10:00",
                         "volume": 30_000_000.0, "turnover": 3_000_000_000.0},
        }],
    }


def recommend(value, now=None, config=None):
    return recommend_realtime(value, config=config, now=now or value["asof"])


def factors(row):
    return {item["key"]: item for item in row["factors"]}


class RealtimeTests(unittest.TestCase):
    def test_full_evidence_can_only_make_a_nonexecutable_watchlist(self):
        report = recommend(frame())
        row = report["candidates"][0]
        self.assertFalse(report["blocked"])
        self.assertEqual(row["action"], "重点观察")
        self.assertTrue(row["recommendation_eligible"])
        self.assertEqual(row["confidence"], 1)
        self.assertFalse(row["eligible"])
        self.assertFalse(row["execution_ready"])
        self.assertEqual(row["target_weight"], 0)
        self.assertFalse(report["regime"]["allow_entries"])
        self.assertNotIn("orders", report)

    def test_missing_seal_and_open_times_are_not_fabricated(self):
        value = frame()
        full = recommend(value)["candidates"][0]
        value["stocks"][0]["previous"]["final_seal_time"] = None
        value["stocks"][0]["previous"]["open_times"] = None
        row = recommend(value)["candidates"][0]
        parts = factors(row)
        for key in ("previous_open_times", "previous_final_seal_time"):
            self.assertFalse(parts[key]["available"])
            self.assertIsNone(parts[key]["value"])
            self.assertEqual(parts[key]["score"], 0)
        self.assertEqual(full["score"] - row["score"], 20)
        self.assertEqual(row["confidence"], .8)
        self.assertEqual(parts["previous_limit_up_time"]["value"], "09:40")
        self.assertNotIn("首次", parts["previous_limit_up_time"]["label"])

    def test_zero_open_times_is_known_zero_and_not_missing(self):
        row = recommend(frame())["candidates"][0]
        part = factors(row)["previous_open_times"]
        self.assertTrue(part["available"])
        self.assertEqual(part["value"], 0)
        self.assertEqual(part["score"], 15)

    def test_auction_not_ready_is_not_interpreted_as_zero(self):
        value = frame()
        value["stocks"][0]["auction"]["data_status"] = "not_ready"
        row = recommend(value)["candidates"][0]
        for part in row["factors"]:
            if part["key"].startswith("auction_"):
                self.assertFalse(part["available"])
                self.assertIsNone(part["value"])
        self.assertFalse(row["auction_usable"])
        self.assertLess(row["confidence"], .6)
        self.assertEqual(row["action"], "等待确认")

    def test_pre_0925_cannot_use_final_auction(self):
        value = frame("09:20:00", "auction")
        value["stocks"][0]["auction"]["asof"] = value["asof"]
        row = recommend(value)["candidates"][0]
        self.assertFalse(row["auction_usable"])
        self.assertEqual(row["action"], "等待确认")

    def test_same_day_final_auction_remains_context_in_afternoon(self):
        value = frame("14:00:00")
        row = recommend(value)["candidates"][0]
        self.assertTrue(row["auction_usable"])
        self.assertTrue(factors(row)["auction_amount"]["available"])

    def test_live_auction_after_open_is_not_reused_as_final(self):
        value = frame()
        value["stocks"][0]["auction"]["auction_phase"] = "live"
        row = recommend(value)["candidates"][0]
        self.assertFalse(row["auction_usable"])

    def test_auction_turnover_is_not_intraday_turnover_proxy(self):
        value = frame()
        value["stocks"][0]["current_turnover_pct"] = None
        row = recommend(value)["candidates"][0]
        parts = factors(row)
        self.assertTrue(parts["auction_turnover"]["available"])
        self.assertFalse(parts["current_turnover"]["available"])
        self.assertIsNone(parts["current_turnover"]["value"])
        self.assertEqual(row["confidence"], .9)

    def test_daily_participation_is_separate_from_time_normalized_heuristic(self):
        row = recommend(frame())["candidates"][0]
        self.assertEqual(row["volume_participation_pct"], 25)
        self.assertEqual(row["normalized_volume_pace"], 1)
        self.assertEqual(row["continuous_minutes_elapsed"], 60)
        self.assertIn("非真实历史同期量比", factors(row)["volume_pace"]["reason"])

    def test_lunch_minutes_are_excluded_and_auction_is_not_extrapolated(self):
        value = frame("13:30:00")
        value["stocks"][0]["current_volume"] = 18_750_000
        row = recommend(value)["candidates"][0]
        self.assertEqual(row["continuous_minutes_elapsed"], 150)
        self.assertEqual(row["normalized_volume_pace"], 1)
        auction = frame("09:25:00", "auction")
        row = recommend(auction)["candidates"][0]
        self.assertIsNone(row["normalized_volume_pace"])
        self.assertEqual(factors(row)["volume_pace"]["value"], 1)

    def test_opening_first_minutes_do_not_explode_extrapolation(self):
        row = recommend(frame("09:31:00"))["candidates"][0]
        self.assertIsNone(row["normalized_volume_pace"])
        self.assertFalse(factors(row)["volume_pace"]["available"])
        self.assertIsNotNone(row["volume_participation_pct"])

    def test_fresh_batch_does_not_prove_individual_freshness(self):
        for quote_time in (None, "2026-09-30T10:00:00+08:00", "2026-09-30T10:31:00+08:00"):
            value = frame()
            value["stocks"][0]["quote_asof"] = quote_time
            row = recommend(value)["candidates"][0]
            self.assertEqual(row["action"], "等待确认")
            self.assertFalse(row["individual_quote_fresh"])

    def test_old_batch_is_blocked_even_if_capture_is_fresh(self):
        value = frame()
        value["market_asof"] = "2026-09-30T10:00:00+08:00"
        report = recommend(value)
        self.assertTrue(report["blocked"])
        self.assertEqual(report["candidates"][0]["action"], "等待确认")

    def test_future_capture_previous_evidence_or_batch_is_blocked(self):
        for field in ("available_at", "previous_available_at", "market_asof"):
            value = frame()
            value[field] = "2026-09-30T10:31:00+08:00"
            self.assertTrue(recommend(value)["blocked"])

    def test_closed_lunch_holiday_and_unverified_days_block_recommendations(self):
        for clock, phase in (("08:00:00", "preopen"), ("09:26:00", "auction"),
                             ("12:00:00", "lunch"), ("15:01:00", "closed")):
            value = frame(clock, phase)
            self.assertTrue(recommend(value)["blocked"])
        for verified in (False, None, 1, "true"):
            value = frame()
            value["is_trading_day"] = verified
            self.assertTrue(recommend(value)["blocked"])

    def test_malformed_numbers_do_not_inflate_score_or_break_json(self):
        for field in ("current_volume", "current_turnover_pct"):
            for bad in (True, float("nan"), float("inf"), -1):
                value = frame()
                value["stocks"][0][field] = bad
                report = recommend(value)
                json.dumps(report, allow_nan=False)
                row = report["candidates"][0]
                self.assertTrue(0 <= row["score"] <= 100)
                self.assertLess(row["confidence"], 1)

    def test_supported_scope_and_risk_flags_are_explainable(self):
        for field, value in (("symbol", "300001.SZ"), ("is_st", True), ("is_new", True), ("suspended", True)):
            sample = frame()
            sample["stocks"][0][field] = value
            self.assertEqual(recommend(sample)["candidates"][0]["action"], "回避")
        sample = frame()
        sample["stocks"][0]["is_st"] = 0
        self.assertEqual(recommend(sample)["candidates"][0]["action"], "等待确认")

    def test_snapshot_rejections_and_date_mismatch(self):
        for field, bad in (("schema_version", True), ("previous_trading_date", "2026-09-30"),
                           ("trading_date", "2026-09-29"), ("source", "unknown")):
            value = frame()
            value[field] = bad
            self.assertTrue(recommend(value)["blocked"])
        value = frame()
        value["stocks"].append(copy.deepcopy(value["stocks"][0]))
        self.assertTrue(recommend(value)["blocked"])

    def test_inputs_and_global_weights_are_not_mutated(self):
        value = frame()
        original = copy.deepcopy(value)
        report = recommend(value)
        self.assertEqual(value, original)
        report["recommendation_config"]["weights"]["auction_gap"] = 999
        self.assertEqual(recommend(value)["recommendation_config"]["weights"]["auction_gap"], 15)
        other = recommend(value, now=datetime(2026, 9, 30, 2, 30, tzinfo=timezone.utc))
        self.assertEqual(other, recommend(value))


if __name__ == "__main__":
    unittest.main()
