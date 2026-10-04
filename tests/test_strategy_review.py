"""Independent causality, data-boundary, and portfolio-invariant checks."""
import copy
from datetime import datetime, timezone
import unittest

from dragon_agent.config import validate_config
from dragon_agent.strategy import analyze


def snapshot():
    rows = []
    for index in range(8):
        rows.append({
            "symbol": f"600{index:03d}.SH", "name": f"测试 {index}",
            "price": 11.0, "prev_close": 10.0, "change_pct": 10.0,
            "turnover": 1_500_000_000.0, "turnover_pct": 12.0,
            "board_count": 4, "seal_money": 100_000_000.0,
            "max_seal_money": 100_000_000.0,
            "theme": "测试方向甲" if index < 4 else "测试方向乙",
            "first_limit_time": "09:30", "is_st": False, "is_new": False,
            "suspended": False, "quote_asof": "2026-09-30T15:00:00+08:00",
            "limit_up": 11.0, "limit_down": 9.0,
        })
    return {
        "schema_version": 1, "source": "demo", "trading_date": "2026-09-30",
        "asof": "2026-09-30T15:00:00+08:00",
        "available_at": "2026-09-30T15:01:00+08:00",
        "coverage": {"complete": True, "issues": []},
        "market": {"limit_up_count": 70, "limit_down_count": 3,
                   "broken_count": 10, "max_height": 5},
        "stocks": rows,
    }


def research(value, **kwargs):
    return analyze(value, now="2026-09-30T15:02:00+08:00", **kwargs)


class StrategyReviewTests(unittest.TestCase):
    def test_does_not_mutate_evidence_or_config(self):
        value, config = snapshot(), {"max_theme_weight": 0.25}
        original, config_original = copy.deepcopy(value), dict(config)
        research(value, config=config)
        self.assertEqual(value, original)
        self.assertEqual(config, config_original)

    def test_portfolio_budgets_hold_across_regimes_and_constraints(self):
        for market in (
            {"limit_up_count": 70, "limit_down_count": 3, "broken_count": 10, "max_height": 5},
            {"limit_up_count": 30, "limit_down_count": 6, "broken_count": 7, "max_height": 3},
            {"limit_up_count": 17, "limit_down_count": 9, "broken_count": 10, "max_height": 3},
            {"limit_up_count": 70, "limit_down_count": 16, "broken_count": 10, "max_height": 5},
            {"limit_up_count": 7, "limit_down_count": 1, "broken_count": 2, "max_height": 2},
        ):
            for cap in (0.23, 0.40, 0.60):
                for theme_cap in (0.10, 0.25, 0.35):
                    value = snapshot()
                    value["market"] = market
                    report = research(value, config={"max_exposure": cap, "max_theme_weight": theme_cap})
                    selected = [row for row in report["candidates"] if row["target_weight"] > 0]
                    with self.subTest(market=market, cap=cap, theme_cap=theme_cap):
                        self.assertLessEqual(len(selected), 3)
                        self.assertLessEqual(sum(x["target_weight"] for x in selected), report["regime"]["exposure_cap"] + 1e-6)
                        self.assertTrue(all(0 < x["target_weight"] <= 0.20 for x in selected))
                        for theme in {x["theme"] for x in selected}:
                            self.assertLessEqual(sum(x["target_weight"] for x in selected if x["theme"] == theme), theme_cap + 1e-6)

    def test_unknown_execution_metadata_keeps_research_conditional(self):
        value = snapshot()
        for field in ("suspended", "limit_up", "limit_down", "prev_close", "quote_asof"):
            value["stocks"][0][field] = None
        row = research(value)["candidates"][0]
        self.assertTrue(row["eligible"])
        self.assertFalse(row["execution_ready"])
        self.assertGreater(row["target_weight"], 0)
        self.assertTrue(row["execution_blocks"])

    def test_stock_risk_flags_require_actual_false(self):
        for field in ("is_st", "is_new"):
            for invalid in (None, 0, "false", [], True):
                value = snapshot()
                value["stocks"][0][field] = invalid
                rows = research(value)["candidates"]
                row = next(x for x in rows if x["symbol"] == "600000.SH")
                with self.subTest(field=field, invalid=invalid):
                    self.assertFalse(row["eligible"])
                    self.assertEqual(row["target_weight"], 0)

    def test_incomplete_or_invalid_market_blocks_all_budgets(self):
        for field in ("limit_up_count", "limit_down_count", "broken_count", "max_height"):
            for invalid in (None, True, -1, 0.5, float("nan"), float("inf"), "70"):
                value = snapshot()
                value["market"][field] = invalid
                report = research(value)
                with self.subTest(field=field, invalid=invalid):
                    self.assertTrue(report["blocked"])
                    self.assertFalse(report["regime"]["allow_entries"])
                    self.assertTrue(all(x["target_weight"] == 0 for x in report["candidates"]))
        value = snapshot()
        value["coverage"]["complete"] = 1
        self.assertTrue(research(value)["blocked"])

    def test_historical_flag_never_makes_future_data_available(self):
        value = snapshot()
        value["available_at"] = "2026-10-01T16:00:00+08:00"
        for historical in (False, True):
            report = research(value, historical=historical)
            self.assertTrue(report["blocked"])
            self.assertTrue(all(x["target_weight"] == 0 for x in report["candidates"]))

    def test_historical_only_disables_wall_clock_age(self):
        value = snapshot()
        old_now = "2026-10-03T15:02:00+08:00"
        self.assertTrue(analyze(value, now=old_now)["blocked"])
        self.assertFalse(analyze(value, now=old_now, historical=True)["blocked"])
        value["asof"] = "2026-09-30T15:03:00+08:00"
        self.assertTrue(analyze(value, now=old_now, historical=True)["blocked"])

    def test_future_quote_cannot_be_used_for_ranking_budget(self):
        value = snapshot()
        value["stocks"][0]["quote_asof"] = "2026-09-30T15:01:01+08:00"
        row = next(x for x in research(value)["candidates"] if x["symbol"] == "600000.SH")
        self.assertFalse(row["eligible"])
        self.assertEqual(row["target_weight"], 0)

    def test_duplicate_symbols_block_the_snapshot(self):
        value = snapshot()
        value["stocks"].append(copy.deepcopy(value["stocks"][0]))
        self.assertTrue(research(value)["blocked"])

    def test_timezone_equivalent_decision_produces_same_result(self):
        value = snapshot()
        report = analyze(value, now=datetime(2026, 9, 30, 7, 2, tzinfo=timezone.utc))
        self.assertEqual(report, research(value))

    def test_nonfinite_and_boolean_numbers_do_not_produce_nan_scores(self):
        for field in ("board_count", "seal_money", "turnover", "price"):
            for invalid in (float("nan"), float("inf"), True):
                value = snapshot()
                value["stocks"][0][field] = invalid
                row = next(x for x in research(value)["candidates"] if x["symbol"] == "600000.SH")
                with self.subTest(field=field, invalid=invalid):
                    self.assertFalse(row["eligible"])
                    self.assertTrue(0 <= row["score"] <= 100)

    def test_config_refuses_silent_typos_and_unsafe_types(self):
        for config in ({"max_posiitons": 100}, {"max_positions": True}, {"max_exposure": float("nan")},
                       {"max_positions": 1.5}, {"initial_capital": 0}, {"max_daily_loss": 2},
                       {"commission_rate": -0.1}, {"slippage_bps": 1001}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate_config(config)

    def test_boolean_schema_version_is_rejected(self):
        value = snapshot()
        value["schema_version"] = True
        self.assertTrue(research(value)["blocked"])

    def test_first_limit_time_cannot_be_after_snapshot_time(self):
        value = snapshot()
        value["asof"] = "2026-09-30T09:35:00+08:00"
        value["available_at"] = "2026-09-30T09:35:01+08:00"
        for row in value["stocks"]:
            row["quote_asof"] = value["asof"]
        value["stocks"][0]["first_limit_time"] = "10:00"
        report = analyze(value, now="2026-09-30T09:35:02+08:00")
        row = next(x for x in report["candidates"] if x["symbol"] == "600000.SH")
        self.assertFalse(row["eligible"])

    def test_lunch_break_is_not_a_valid_first_limit_time(self):
        value = snapshot()
        value["stocks"][0]["first_limit_time"] = "12:00"
        row = next(x for x in research(value)["candidates"] if x["symbol"] == "600000.SH")
        self.assertFalse(row["eligible"])


if __name__ == "__main__":
    unittest.main()
