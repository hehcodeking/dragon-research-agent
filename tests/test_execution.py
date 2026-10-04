import json
from pathlib import Path
import tempfile
import unittest

from dragon_agent.execution import DisabledLiveBroker, PaperBroker, decimal, validate_adapter


DAY1 = "2026-09-28"
DAY2 = "2026-09-29"
AT1 = DAY1 + "T09:30:00+08:00"
AT2 = DAY2 + "T09:30:00+08:00"
ZERO_COST = {"slippage_bps": 0, "commission_rate": 0, "min_commission": 0,
             "stamp_tax_rate": 0, "transfer_fee_rate": 0}


def quote(symbol="600001.SH", price=10, at=AT1, **overrides):
    return {"symbol": symbol, "price": price, "prev_close": 10, "limit_up": 11,
            "limit_down": 9, "quote_asof": at, "is_st": False, "is_new": False,
            "suspended": False, **overrides}


class ExecutionTests(unittest.TestCase):
    def test_buy_lots_and_t_plus_one(self):
        broker = PaperBroker(config=ZERO_COST)
        self.assertEqual(broker.submit("bad-lot", "600001.SH", "buy", 150, quote(), AT1)["status"], "rejected")
        self.assertEqual(broker.submit("buy", "600001.SH", "buy", 2000, quote(), AT1)["status"], "filled")
        rejected = broker.submit("today-sell", "600001.SH", "sell", 100, quote(), AT1)
        self.assertIn("t_plus_one", rejected["reason"])
        self.assertEqual(broker.status()["positions"]["600001.SH"]["sellable_quantity"], 0)
        broker.mark_market([quote(at=AT2)], DAY2, AT2)
        self.assertEqual(broker.submit("odd-sell", "600001.SH", "sell", 50, quote(at=AT2), AT2)["status"], "rejected")
        sold = broker.submit("next-sell", "600001.SH", "sell", 2000, quote(at=AT2), AT2)
        self.assertEqual(sold["status"], "filled")
        self.assertEqual(broker.status()["cash"], "100000.00")

    def test_limits_flags_and_unsupported_market_reject(self):
        broker = PaperBroker()
        cases = [quote(price=11), quote(limit_up=None), quote(suspended=True),
                 quote(is_st=None), quote(is_new=0), quote(price=float("nan")),
                 quote(symbol="300001.SZ"), quote(quote_asof=DAY1 + "T09:31:00+08:00"),
                 quote(quote_asof=DAY1 + "T09:00:00+08:00")]
        for index, row in enumerate(cases):
            with self.subTest(row=row):
                result = broker.submit(str(index), row["symbol"], "buy", 100, row, AT1)
                self.assertEqual(result["status"], "rejected")
        self.assertEqual(broker.status()["cash"], "100000.00")

    def test_timestamps_and_session_are_strict(self):
        broker = PaperBroker()
        for index, at in enumerate(["2026-09-28T09:30:00", "20260928T093000+0800",
                                    "2026-09-28T12:00:00+08:00", "2026-09-27T09:30:00+08:00"]):
            result = broker.submit(f"bad-{index}", "600001.SH", "buy", 100, quote(at=at), at)
            self.assertEqual(result["status"], "rejected")
        broker.mark_market([], DAY2, AT2)
        self.assertEqual(broker.submit("backwards", "600001.SH", "buy", 100, quote(), AT1)["status"], "rejected")
        with self.assertRaises(ValueError):
            broker.mark_market([], "2026-9-29", AT2)
        with self.assertRaises(ValueError):
            broker.mark_market([], DAY1, AT2)

    def test_idempotency_atomic_persistence_and_reload(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ledger.json"
            one = PaperBroker(path)
            two = PaperBroker(path)
            first = one.submit("stable", "600001.SH", "buy", 1900, quote(), AT1)
            self.assertEqual(first["status"], "filled")
            duplicate = two.submit("stable", "600001.SH", "buy", 1900, quote(price=10.5), AT1)
            self.assertEqual(first, duplicate)
            with self.assertRaises(ValueError):
                two.submit("stable", "600001.SH", "buy", 1800, quote(), AT1)
            next_fill = two.submit("other", "600002.SH", "buy", 1900, quote(symbol="600002.SH"), AT1)
            self.assertEqual(next_fill["status"], "filled")
            status = one.status()  # stale in-memory instance must reload under lock
            saved = json.loads(path.read_text())
            self.assertEqual(len(status["positions"]), 2)
            self.assertEqual(status["cash"], saved["cash"])
            self.assertEqual(len(status["orders"]), 2)
            self.assertFalse(list(Path(temp).glob("*.tmp")))
            with self.assertRaises(ValueError):
                PaperBroker(path, {"initial_capital": 200000})

    def test_fee_slippage_and_post_trade_position_cap(self):
        broker = PaperBroker()
        reject = broker.submit("too-big", "600001.SH", "buy", 2000, quote(), AT1)
        self.assertEqual(reject["reason"], "max_stock_weight")
        fill = broker.submit("fit", "600001.SH", "buy", 1900, quote(), AT1)
        self.assertEqual(fill["fill_price"], "10.01")
        self.assertGreater(decimal(fill["fees"]), 0)
        equity = decimal(broker.status()["equity"])
        self.assertEqual(equity, decimal(100000) - decimal(fill["fees"]) - 19)
        broker.mark_market([quote(at=AT2)], DAY2, AT2)
        sell = broker.submit("sell", "600001.SH", "sell", 1900, quote(at=AT2), AT2)
        self.assertEqual(sell["fill_price"], "9.99")
        self.assertGreater(decimal(sell["stamp_tax"]), 0)
        self.assertLess(decimal(broker.status()["cash"]), 100000)

    def test_exposure_cap_and_missing_marks(self):
        broker = PaperBroker(config=ZERO_COST)
        broker.submit("one", "600001.SH", "buy", 2000, quote(), AT1)
        reject = broker.submit("regime-cap", "600002.SH", "buy", 100, quote(symbol="600002.SH"), AT1, exposure_cap=.2)
        self.assertEqual(reject["reason"], "max_exposure")
        marked = broker.mark_market([], DAY2, AT2)
        self.assertEqual(marked["equity"], "100000.00")
        self.assertFalse(marked["allow_entries"])
        reject = broker.submit("missing-holding", "600002.SH", "buy", 100, quote(symbol="600002.SH", at=AT2), AT2)
        self.assertIn("stale_position_marks", reject["reason"])

    def test_old_quotes_cannot_be_refreshed_by_mark_call(self):
        broker = PaperBroker(config=ZERO_COST)
        broker.submit("one", "600001.SH", "buy", 100, quote(), AT1)
        at32 = DAY1 + "T09:32:00+08:00"
        broker.mark_market([quote()], DAY1, at32)
        self.assertEqual(broker.status()["positions"]["600001.SH"]["mark_asof"], AT1)
        at34 = DAY1 + "T09:34:00+08:00"
        result = broker.submit("two", "600002.SH", "buy", 100, quote(symbol="600002.SH", at=at34), at34)
        self.assertIn("stale_position_marks", result["reason"])

    def test_marked_losses_latch_risk_but_allow_liquidation(self):
        broker = PaperBroker(config=ZERO_COST)
        broker.submit("one", "600001.SH", "buy", 2000, quote(), AT1)
        low = quote(price=5, at=AT2, prev_close=5, limit_up=5.5, limit_down=4.5)
        risk = broker.mark_market([low], DAY2, AT2)
        self.assertEqual(risk["equity"], "90000.00")
        self.assertIn("daily_loss_limit", risk["reasons"])
        self.assertIn("max_drawdown_limit", risk["reasons"])
        rejected = broker.submit("new", "600002.SH", "buy", 100, quote(symbol="600002.SH", at=AT2), AT2)
        self.assertIn("risk_block", rejected["reason"])
        self.assertEqual(broker.submit("exit", "600001.SH", "sell", 2000, low, AT2)["status"], "filled")
        day3, at3 = "2026-09-30", "2026-09-30T09:30:00+08:00"
        risk3 = broker.mark_market([], day3, at3)
        self.assertNotIn("daily_loss_limit", risk3["reasons"])
        self.assertIn("max_drawdown_limit", risk3["reasons"])

    def test_gap_loss_and_limit_down_cannot_force_fill(self):
        broker = PaperBroker(config=ZERO_COST)
        broker.submit("one", "600001.SH", "buy", 2000, quote(), AT1)
        low = quote(price=9, at=AT2)
        risk = broker.mark_market([low], DAY2, AT2)
        self.assertGreaterEqual(risk["daily_loss"], .02)
        self.assertEqual(broker.submit("exit", "600001.SH", "sell", 2000, low, AT2)["reason"], "limit_board_unfilled")

    def test_live_disabled_and_bad_config(self):
        live = DisabledLiveBroker("reserved-qmt")
        self.assertFalse(validate_adapter(live)["ready"])
        self.assertFalse(validate_adapter(object())["ready"])
        with self.assertRaises(RuntimeError): live.submit(symbol="600001.SH")
        with self.assertRaises(RuntimeError): live.cancel("id")
        for key, value in [("initial_capital", "NaN"), ("slippage_bps", float("inf")),
                           ("max_exposure", 1.1), ("max_positions", True)]:
            with self.subTest(key=key):
                with self.assertRaises(ValueError): PaperBroker(config={key: value})


if __name__ == "__main__":
    unittest.main()
