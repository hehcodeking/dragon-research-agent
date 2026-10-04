from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from dragon_agent.data import SHANGHAI, _pool
from dragon_agent.realtime_data import _validated_calendar
from test_connection import Client, CALENDAR_PATH, fixtures, NOW, verify_connection, ms
from test_realtime_data import Client as RealtimeClient, RealtimeCollector


class LiveSemanticsTests(unittest.TestCase):
    def test_subsecond_calendar_clock_skew_is_metadata_only(self):
        client = Client()
        client.values[CALENDAR_PATH]["timestamp"] = ms(NOW) + 61
        with patch("dragon_agent.connection._now", return_value=NOW):
            report = verify_connection(client)
        self.assertTrue(report["is_trading_day"])
        self.assertIn("calendar:assembly_clock_skew_within_2_seconds", report["issues"])
        self.assertFalse(report["per_symbol_freshness_verified"])
        client.values[CALENDAR_PATH]["timestamp"] = ms(NOW) + 2001
        with self.assertRaises(Exception):
            _validated_calendar(client.values[CALENDAR_PATH], NOW)

    def test_historical_pool_current_assembly_time_is_not_market_time(self):
        class PoolClient:
            def get(self, path, params):
                self.params = params
                return {"timestamp": ms(NOW) + 61,
                        "pagination": {"total": 0, "pages": 0, "page": 1, "size": 200}, "item": []}
        client = PoolClient()
        with patch("dragon_agent.data._now", return_value=NOW):
            result = _pool(client, "limit-up-pool", NOW.date() - timedelta(days=1))
        self.assertTrue(result["complete"])
        self.assertFalse(result["market_timestamp_verified"])
        self.assertEqual(result["available_at"], NOW.isoformat())
        self.assertEqual(result["requested_trading_date"], "2026-09-29")

    def test_final_auction_gap_collects_but_does_not_enable_legacy_trading(self):
        client = RealtimeClient()
        now = datetime(2026, 9, 18, 9, 26, tzinfo=SHANGHAI)
        client.calendar_time = "2026-09-18T09:26:00"
        with patch("dragon_agent.realtime_data._now", return_value=now):
            frame = RealtimeCollector(client, 2, 0, sleep=lambda _: None).collect()
        self.assertEqual(frame["session_status"], "preopen")
        self.assertTrue(any(path.endswith("/auction/snapshot") for path, _ in client.calls))
