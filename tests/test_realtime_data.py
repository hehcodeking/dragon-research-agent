import copy
from datetime import datetime
import unittest
from unittest.mock import patch

from dragon_agent.data import ApiError, DataError, SHANGHAI
from dragon_agent.realtime_data import RealtimeCollector, session_status

NOW = datetime(2026, 9, 18, 9, 50, tzinfo=SHANGHAI)


def ms(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=SHANGHAI).timestamp() * 1000)


class Client:
    def __init__(self):
        self.calls, self.audit = [], []
        self.history_wrong_date = False
        self.quote_old = False
        self.calendar_missing = False
        self.duplicate_quotes = False
        self.future_history_timestamp = False
        self.turnover_pool_time = None
        self.calendar_time = "2026-09-18T09:50:00"
        self.calendar_days = ("2026-09-17", "2026-09-18")
        self.calendar_utc_midnight = False

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path.endswith("trading-days"):
            if self.calendar_missing:
                raise DataError("unavailable")
            return {"timestamp": ms(self.calendar_time), "item": [
                {"date": day.replace("-", ""), "date_ms": ms(day + "T00:00:00")
                 + (8 * 3600 * 1000 if self.calendar_utc_midnight else 0)}
                for day in self.calendar_days]}
        if path.endswith("historical"):
            day = "2026-09-18" if self.history_wrong_date else "2026-09-17"
            return {"timestamp": ms("2026-09-18T15:00:00" if self.future_history_timestamp else day + "T15:00:00"), "item": [{"date_ms": ms(day + "T00:00:00"),
                "volume": 50000000, "turnover": 800000000}]}
        if path.endswith("/auction/snapshot"):
            return {"timestamp": ms("2026-09-18T09:50:00"), "data_status": "ready", "auction_phase": "final",
                    "item": [{"thscode": "600001.SH", "auction_pct": 3, "auction_turnover_pct": .5,
                              "auction_volume": 1000000, "auction_amount": 15000000}]}
        if path.endswith("/prices/snapshot"):
            rows = [{"thscode": "600001.SH", "last_price": 15.8, "prev_price": 15, "volume": 7000000, "turnover": 100000000}]
            return {"timestamp": ms("2026-09-17T15:00:00" if self.quote_old else "2026-09-18T09:50:00"),
                    "item": rows * (2 if self.duplicate_quotes else 1)}
        requested = datetime.fromtimestamp(params["date_ms"] / 1000, SHANGHAI).date().isoformat()
        prior_up = path.endswith("limit-up-pool") and requested == "2026-09-17"
        rows = [{"thscode": "600001.SH", "name": "测试", "continue_day_cnt": 3, "seal_money": 1e8,
                 "limit_up_time": "09:40", "is_st": False, "is_new": False}] if prior_up else []
        timestamp = requested + ("T15:00:00" if requested == "2026-09-17" else "T09:50:00")
        if self.turnover_pool_time and requested == "2026-09-18":
            rows = [{"thscode": "600001.SH", "turnover_ratio_pct": 7.5}]
            timestamp = "2026-09-18T" + self.turnover_pool_time
        return {"timestamp": ms(timestamp),
                "pagination": {"total": len(rows), "pages": len(rows), "size": 200, "page": 1}, "item": rows}


class RealtimeDataTests(unittest.TestCase):
    def collect(self, client):
        with patch("dragon_agent.realtime_data._now", return_value=NOW):
            return RealtimeCollector(client, max_symbols=2, history_limit=2, sleep=lambda _: None).collect()

    def test_preserve_field_semantics_and_unknowns(self):
        frame = self.collect(Client())
        stock = frame["stocks"][0]
        self.assertEqual(frame["previous_trading_date"], "2026-09-17")
        self.assertEqual(stock["auction"]["auction_turnover_pct"], .5)
        self.assertIsNone(stock["current_turnover_pct"])
        self.assertIsNone(stock["previous"]["open_times"])
        self.assertIsNone(stock["previous"]["final_seal_time"])
        self.assertEqual(stock["previous"]["limit_up_time"], "09:40")
        self.assertEqual(stock["previous"]["volume"], 50000000)
        self.assertIsNone(stock["quote_asof"])
        self.assertFalse(stock["execution_verified"])

    def test_previous_context_cached_but_today_always_refreshed(self):
        client = Client()
        collector = RealtimeCollector(client, 2, 2, sleep=lambda _: None)
        with patch("dragon_agent.realtime_data._now", return_value=NOW):
            collector.collect()
            collector.collect()
        self.assertEqual(sum(p.endswith("historical") for p, _ in client.calls), 1)
        self.assertEqual(sum(p.endswith("/prices/snapshot") for p, _ in client.calls), 2)

    def test_mismatched_previous_bar_is_not_used(self):
        client = Client()
        client.history_wrong_date = True
        self.assertIsNone(self.collect(client)["stocks"][0]["previous"]["volume"])

    def test_old_or_duplicate_quotes_not_used(self):
        for field in ("quote_old", "duplicate_quotes"):
            client = Client()
            setattr(client, field, True)
            frame = self.collect(client)
            self.assertIsNone(frame["stocks"][0]["price"])
            self.assertIsNone(frame["market_asof"])

    def test_missing_calendar_stops_data_requests(self):
        client = Client()
        client.calendar_missing = True
        frame = self.collect(client)
        self.assertFalse(frame["coverage"]["complete"])
        self.assertEqual(frame["stocks"], [])
        self.assertEqual(len(client.calls), 1)

    def test_authentication_failure_is_not_misreported_as_closed_market(self):
        client = Client()
        with patch.object(client, "get", side_effect=ApiError(2003)), self.assertRaises(ApiError):
            self.collect(client)

    def test_verified_holiday_does_not_fetch_stock_pools(self):
        client = Client()
        client.calendar_time = "2026-09-19T09:50:00"
        with patch("dragon_agent.realtime_data._now", return_value=NOW.replace(day=19)):
            frame = RealtimeCollector(client, 2, 2, sleep=lambda _: None).collect()
        self.assertIs(frame["is_trading_day"], False)
        self.assertEqual(frame["session_status"], "closed")
        self.assertEqual(len(client.calls), 1)

    def test_undocumented_utc_midnight_encoding_is_rejected(self):
        client = Client()
        client.calendar_utc_midnight = True
        frame = self.collect(client)
        self.assertIsNone(frame["is_trading_day"])
        self.assertEqual(frame["stocks"], [])

    def test_stale_or_future_calendar_source_does_not_enable_collection(self):
        for source in ("2026-09-17T15:00:00", "2026-09-18T09:50:03", "2026-09-19T00:00:00"):
            with self.subTest(source=source):
                client = Client()
                client.calendar_time = source
                frame = self.collect(client)
                self.assertIsNone(frame["is_trading_day"])
                self.assertEqual(frame["stocks"], [])
                self.assertEqual(len(client.calls), 1)

    def test_future_calendar_session_does_not_prove_coverage(self):
        client = Client()
        client.calendar_days += ("2026-09-21",)
        frame = self.collect(client)
        self.assertIsNone(frame["is_trading_day"])
        self.assertEqual(len(client.calls), 1)

    def test_prior_day_calendar_does_not_prove_holiday(self):
        client = Client()
        with patch("dragon_agent.realtime_data._now", return_value=NOW.replace(day=19)):
            frame = RealtimeCollector(client, 2, 2, sleep=lambda _: None).collect()
        self.assertIsNone(frame["is_trading_day"])
        self.assertEqual(frame["session_status"], "unknown")
        self.assertEqual(len(client.calls), 1)

    def test_historical_realtime_collection_refused(self):
        with patch("dragon_agent.realtime_data._now", return_value=NOW), self.assertRaises(DataError):
            RealtimeCollector(Client()).collect("2026-09-17")

    def test_lunch_not_continuous(self):
        self.assertEqual(session_status(NOW.replace(hour=12), True), "lunch")

    def test_future_history_source_timestamp_not_used(self):
        client = Client()
        client.future_history_timestamp = True
        self.assertIsNone(self.collect(client)["stocks"][0]["previous"]["volume"])

    def test_stale_or_future_turnover_source_never_counts_as_current(self):
        for stamp in ("09:00:00", "15:00:00"):
            client = Client()
            client.turnover_pool_time = stamp
            self.assertIsNone(self.collect(client)["stocks"][0]["current_turnover_pct"])
        client = Client()
        client.turnover_pool_time = "09:49:00"
        frame = self.collect(client)
        self.assertIsNone(frame["stocks"][0]["current_turnover_pct"])
        self.assertEqual(frame["stocks"][0]["raw_current_turnover_pct"], 7.5)


if __name__ == "__main__":
    unittest.main()
