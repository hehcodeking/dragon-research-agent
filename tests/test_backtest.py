from copy import deepcopy
from unittest.mock import patch
import unittest

from dragon_agent.backtest import replay


ZERO_COST = {"slippage_bps": 0, "commission_rate": 0, "min_commission": 0,
             "stamp_tax_rate": 0, "transfer_fee_rate": 0, "max_holding_days": 20}


def frame(day, price=10, open_price=10, symbol="600001.SH", **stock_changes):
    at = day + "T15:00:00+08:00"
    return {"schema_version": 1, "source": "imported", "trading_date": day,
            "asof": at, "available_at": at, "coverage": {"complete": True, "issues": []},
            "market": {}, "stocks": [{"symbol": symbol, "name": "Synthetic", "price": price,
            "open": open_price, "prev_close": 10, "limit_up": 11, "limit_down": 9,
            "quote_asof": at, "is_st": False, "is_new": False, "suspended": False, **stock_changes}]}


def signals(snapshot, config=None, now=None, historical=False):
    return {"blocked": False, "regime": {"name": "test", "allow_entries": True, "exposure_cap": .6},
            "candidates": [{"symbol": row["symbol"], "eligible": True, "target_weight": .2}
                           for row in snapshot["stocks"]]}


class ReplayTests(unittest.TestCase):
    def run_replay(self, frames, config=None, analyzer=signals):
        with patch("dragon_agent.strategy.analyze", side_effect=analyzer):
            return replay(frames, {**ZERO_COST, **(config or {})})

    def test_prior_signal_executes_next_open_not_future_close(self):
        frames = [frame("2026-09-28"), frame("2026-09-29", price=10.5, open_price=10.1)]
        result = self.run_replay(frames)
        fill = result["orders"][0]
        self.assertEqual(fill["status"], "filled")
        self.assertEqual(fill["fill_price"], "10.10")
        self.assertEqual(fill["signal_date"], "2026-09-28")
        self.assertEqual(fill["asof"], "2026-09-29T09:30:00+08:00")
        changed = deepcopy(frames)
        changed[1]["stocks"][0].update(price=9.5, board_count=0, theme="future-bad", high=100, low=1, volume=1)
        other = self.run_replay(changed)
        self.assertEqual(result["orders"], other["orders"])
        self.assertNotEqual(result["metrics"]["final_equity"], other["metrics"]["final_equity"])
        self.assertEqual(result["equity_curve"][0]["positions"], {})

    def test_capture_time_after_execution_never_backdated(self):
        frames = [frame("2026-09-28"), frame("2026-09-29")]
        frames[0]["available_at"] = "2026-10-01T16:00:00+08:00"
        result = self.run_replay(frames)
        self.assertEqual(result["metrics"]["filled_orders"], 0)
        self.assertEqual(result["orders"][0]["reason"], "signal_not_available_before_next_session_open")
        frames[0]["available_at"] = "2026-09-29T09:30:00+08:00"
        self.assertEqual(self.run_replay(frames)["metrics"]["filled_orders"], 0)

    def test_locked_open_or_missing_open_is_unfilled(self):
        first = frame("2026-09-28")
        locked = frame("2026-09-29", open_price=11)
        result = self.run_replay([first, locked])
        self.assertEqual(result["orders"][0]["reason"], "limit_board_unfilled")
        missing = frame("2026-09-29", open_price=None)
        result = self.run_replay([first, missing])
        self.assertEqual(result["orders"][0]["reason"], "missing_next_session_open")
        missing_limits = frame("2026-09-29", limit_up=None)
        result = self.run_replay([first, missing_limits])
        self.assertEqual(result["orders"][0]["status"], "rejected")

    def test_missing_holding_retains_mark_and_freezes_new_entry(self):
        frames = [frame("2026-09-28"), frame("2026-09-29"), frame("2026-09-30", symbol="600002.SH")]
        # Offer second symbol on prior close, while removing the first at next open.
        second = deepcopy(frames[1]["stocks"][0])
        second["symbol"] = "600002.SH"
        frames[1]["stocks"].append(second)
        result = self.run_replay(frames)
        self.assertFalse(result["result_valid"])
        self.assertIn("600001.SH", result["final_state"]["positions"])
        self.assertEqual(result["final_state"]["positions"]["600001.SH"]["mark"], "10")
        self.assertGreater(float(result["metrics"]["final_equity"]), 99000)
        second_orders = [o for o in result["orders"] if o["symbol"] == "600002.SH"]
        self.assertEqual(len(second_orders), 1)
        self.assertIn("stale_position_marks", second_orders[0]["reason"])

    def test_close_stop_waits_until_next_session(self):
        frames = [frame("2026-09-28"), frame("2026-09-29", price=9.4),
                  frame("2026-09-30", price=9.2, open_price=9.2)]
        result = self.run_replay(frames)
        sells = [o for o in result["orders"] if o["side"] == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["status"], "filled")
        self.assertEqual(sells[0]["signal_date"], "2026-09-29")
        self.assertEqual(sells[0]["execution_date"], "2026-09-30")
        self.assertEqual(sells[0]["fill_price"], "9.20")
        self.assertIn("close_stop_loss", sells[0]["signal_reason"])

    def test_missing_close_never_creates_stop_on_zero(self):
        frames = [frame("2026-09-28"), frame("2026-09-29"), frame("2026-09-30")]
        frames[1]["stocks"][0]["price"] = None
        result = self.run_replay(frames)
        self.assertFalse(result["result_valid"])
        self.assertFalse([o for o in result["orders"] if o["side"] == "sell"])
        self.assertEqual(result["metrics"]["final_equity"], "100000.00")

    def test_drawdown_includes_open_gap_even_when_close_recovers(self):
        frames = [frame("2026-09-28"), frame("2026-09-29"),
                  frame("2026-09-30", price=10, open_price=9)]
        result = self.run_replay(frames)
        self.assertGreater(result["metrics"]["max_drawdown"], .018)
        self.assertEqual(result["metrics"]["total_return"], 0)

    def test_invalid_frames_rejected_and_input_not_mutated(self):
        frames = [frame("2026-09-28"), frame("2026-09-29")]
        original = deepcopy(frames)
        self.run_replay(frames)
        self.assertEqual(frames, original)
        for invalid in ([frames[1], frames[0]], [frames[0], frames[0]], []):
            with self.assertRaises(ValueError): self.run_replay(invalid)
        bad = deepcopy(frames)
        bad[0]["available_at"] = "2026-09-28T14:00:00+08:00"
        with self.assertRaises(ValueError): self.run_replay(bad)
        bad = deepcopy(frames)
        bad[0]["stocks"][0]["open"] = float("inf")
        with self.assertRaises(ValueError): self.run_replay(bad)

    def test_fees_reported_and_final_signals_remain_pending(self):
        frames = [frame("2026-09-28"), frame("2026-09-29")]
        result = self.run_replay(frames, {"commission_rate": .0003, "min_commission": 5,
                                         "transfer_fee_rate": .00001, "max_holding_days": 1})
        self.assertGreater(float(result["metrics"]["total_fees"]), 0)
        self.assertLess(float(result["metrics"]["final_equity"]), 100000)
        self.assertTrue(result["pending_signals"])
        self.assertTrue(result["final_state"]["positions"])


if __name__ == "__main__":
    unittest.main()
