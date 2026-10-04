"""Offline diagnostics: authentication, timestamps, identity and secret handling."""
import copy
import io
import json
import unittest
import urllib.error
from datetime import datetime, timedelta
from unittest.mock import patch

from dragon_agent.connection import (CALENDAR_PATH, QUOTE_PATH, SEARCH_PATH,
                                     verify_connection)
from dragon_agent.data import ApiError, CredentialError, DataError, HithinkClient, SHANGHAI


NOW = datetime(2026, 9, 30, 10, 0, tzinfo=SHANGHAI)
SECRET = "offline-secret-never-print"


def ms(value):
    return int(value.timestamp() * 1000)


def fixtures(now=NOW):
    return {
        SEARCH_PATH: {"timestamp": ms(now - timedelta(days=1)), "item": [{
            "thscode": "600519.SH", "ticker": "600519", "name": SECRET,
            "exchange": "SH", "asset_type": "a-share", "currency": "CNY"}]},
        QUOTE_PATH: {"timestamp": ms(now - timedelta(seconds=20)), "total": 5800,
                     "item": [{"thscode": "600519.SH", "ticker": "600519", "last_price": 1277.8}]},
        CALENDAR_PATH: {"timestamp": ms(now), "item": [
            {"date": "20260929", "date_ms": ms(datetime(2026, 9, 29, tzinfo=SHANGHAI))},
            {"date": "20260930", "date_ms": ms(datetime(2026, 9, 30, tzinfo=SHANGHAI))}]},
    }


class Client:
    """HithinkClient-compatible fixture: returned dictionaries imply code zero."""
    def __init__(self, values=None):
        self.values = copy.deepcopy(values if values is not None else fixtures())
        self.calls = []
        self.audit = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        value = self.values[path]
        if isinstance(value, Exception):
            raise value
        self.audit.append({"path": path, "attempt": 1, "code": 0,
                           "request_id": SECRET, "headers": {"X-api-key": SECRET}})
        return copy.deepcopy(value)


class Response:
    def __init__(self, value):
        self.raw = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def getcode(self):
        return 200

    def read(self, size):
        return self.raw[:size]


class Opener:
    def __init__(self, *values):
        self.values = list(values)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return Response(value)


class ConnectionTests(unittest.TestCase):
    def verify(self, client=None, now=NOW):
        with patch("dragon_agent.connection._now", return_value=now):
            report = verify_connection(client if client is not None else Client())
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertFalse(report["live_orders_sent"])
        self.assertFalse(report["per_symbol_freshness_verified"])
        return report

    def test_uses_only_resolved_symbol_and_three_bounded_read_only_requests(self):
        client = Client()
        report = self.verify(client)
        self.assertEqual(client.calls, [
            (SEARCH_PATH, {"q": "600519", "asset_type": "a-share", "limit": 1}),
            (QUOTE_PATH, {"thscodes": "600519.SH"}), (CALENDAR_PATH, None)])
        self.assertEqual(report["status"], "connected")
        for field in ("connected", "auth_verified", "data_ready", "snapshot_valid"):
            self.assertTrue(report[field])
        self.assertEqual(report["price"], 1277.8)
        self.assertEqual(report["latest_market_date"], "2026-09-30")
        self.assertEqual(report["quote_age_seconds"], 20)
        self.assertEqual(report["freshness"], "recent_snapshot_per_symbol_unverified")
        self.assertEqual(report["session_status"], "continuous")

    def test_missing_credential_is_actionable_without_network(self):
        with patch("dragon_agent.connection.HithinkClient", side_effect=CredentialError(SECRET)) as factory:
            report = verify_connection()
        factory.assert_called_once_with(timeout=8, max_retries=1)
        self.assertEqual(report["status"], "credential_missing_or_invalid")
        self.assertIn("connect.command", report["next_step"])
        self.assertFalse(report["auth_verified"])
        self.assertNotIn(SECRET, json.dumps(report))

    def test_http_200_auth_business_errors_are_not_verified(self):
        for code in (2001, 2003):
            with self.subTest(code=code):
                opener = Opener({"code": code, "data": None, "message": SECRET, "request_id": SECRET})
                client = HithinkClient(SECRET, opener=opener, max_retries=0)
                report = self.verify(client)
                self.assertEqual(report["status"], "auth_failed")
                self.assertEqual(report["business_code"], code)
                self.assertFalse(report["auth_verified"])
                self.assertFalse(report["data_ready"])
                self.assertEqual(len(opener.calls), 1)

    def test_successful_auth_does_not_make_missing_quote_ready(self):
        for code, status in ((3002, "data_not_ready"), (4001, "rate_limited"), (5001, "api_error"), (2003, "auth_failed")):
            with self.subTest(code=code):
                client = Client()
                client.values[QUOTE_PATH] = ApiError(code, SECRET)
                report = self.verify(client)
                self.assertEqual(report["status"], status)
                self.assertTrue(report["auth_verified"])
                self.assertFalse(report["snapshot_valid"])
                self.assertFalse(report["data_ready"])
                self.assertIsNone(report["price"])
                self.assertEqual(len(client.calls), 2)

    def test_network_errors_are_separate_and_do_not_leak(self):
        opener = Opener(urllib.error.URLError(SECRET))
        client = HithinkClient(SECRET, opener=opener, max_retries=0)
        report = self.verify(client)
        self.assertEqual(report["status"], "network_error")
        self.assertFalse(report["auth_verified"])

    def test_http_errors_are_separate_and_do_not_leak(self):
        for status, expected in ((401, "auth_failed"), (403, "auth_failed"), (502, "http_error")):
            with self.subTest(status=status):
                error = urllib.error.HTTPError("https://example.invalid/?key=" + SECRET,
                    status, SECRET, {}, io.BytesIO(SECRET.encode()))
                report = self.verify(HithinkClient(SECRET, opener=Opener(error), max_retries=0))
                self.assertEqual(report["status"], expected)

    def test_nonzero_or_missing_envelope_cannot_claim_authentication(self):
        for value in ({"data": fixtures()[SEARCH_PATH]}, {"code": True, "data": {}}, {"code": 0, "data": None}):
            with self.subTest(value=value):
                report = self.verify(HithinkClient(SECRET, opener=Opener(value), max_retries=0))
                self.assertFalse(report["auth_verified"])
                self.assertFalse(report["snapshot_valid"])

    def test_empty_ambiguous_and_mismatched_search_stop_before_quote(self):
        valid = fixtures()[SEARCH_PATH]
        rows = valid["item"]
        cases = [[], rows * 2, [None]]
        for field, value in (("ticker", "000001"), ("thscode", "600519"),
                ("thscode", "600519.SH?key=" + SECRET), ("asset_type", "fund-etf"),
                ("currency", "USD"), ("exchange", "SZ")):
            row = dict(rows[0], **{field: value})
            cases.append([row])
        for bad_rows in cases:
            with self.subTest(rows=bad_rows):
                client = Client()
                client.values[SEARCH_PATH]["item"] = bad_rows
                report = self.verify(client)
                self.assertEqual(report["status"], "invalid_response")
                self.assertEqual(len(client.calls), 1)

    def test_invalid_search_timestamp_does_not_query_quote(self):
        for stamp in (None, True, "123", ms(NOW) + 2001, 10**1000):
            with self.subTest(stamp=repr(stamp)[:30]):
                client = Client()
                client.values[SEARCH_PATH]["timestamp"] = stamp
                report = self.verify(client)
                self.assertEqual(report["status"], "invalid_response")
                self.assertEqual(len(client.calls), 1)

    def test_snapshot_rejects_invalid_price(self):
        for price in (None, True, "1277.8", 0, -1, float("nan"), float("inf"), 10**1000):
            with self.subTest(price=repr(price)[:30]):
                client = Client()
                client.values[QUOTE_PATH]["item"][0]["last_price"] = price
                report = self.verify(client)
                self.assertEqual(report["status"], "invalid_response")
                self.assertFalse(report["snapshot_valid"])

    def test_snapshot_rejects_invalid_shape_identity_or_timestamp(self):
        valid = fixtures()[QUOTE_PATH]
        cases = [None, {}, dict(valid, total=True), dict(valid, total=-1), dict(valid, item=[]),
                 dict(valid, item=valid["item"] * 2), dict(valid, timestamp=None),
                 dict(valid, timestamp=ms(NOW) + 1), dict(valid, timestamp=float(ms(NOW))),
                 dict(valid, item=[dict(valid["item"][0], thscode="000001.SZ")]),
                 dict(valid, item=[dict(valid["item"][0], ticker="000001")])]
        for value in cases:
            with self.subTest(value=value):
                client = Client()
                client.values[QUOTE_PATH] = value
                report = self.verify(client)
                self.assertEqual(report["status"], "invalid_response")
                self.assertFalse(report["snapshot_valid"])

    def test_stale_quote_remains_connected_but_is_not_realtime(self):
        for stamp in (NOW - timedelta(minutes=5), NOW - timedelta(days=1)):
            client = Client()
            client.values[QUOTE_PATH]["timestamp"] = ms(stamp)
            report = self.verify(client)
            self.assertEqual(report["status"], "connected")
            self.assertTrue(report["snapshot_valid"])
            self.assertEqual(report["freshness"], "stale_snapshot")

    def test_holiday_can_report_real_latest_session_without_claiming_realtime(self):
        now = datetime(2026, 10, 3, 10, 0, tzinfo=SHANGHAI)
        client = Client(fixtures(now))
        client.values[QUOTE_PATH]["timestamp"] = ms(datetime(2026, 9, 30, 15, 0, tzinfo=SHANGHAI))
        report = self.verify(client, now)
        self.assertFalse(report["is_trading_day"])
        self.assertEqual(report["session_status"], "closed")
        self.assertEqual(report["latest_market_date"], "2026-09-30")
        self.assertEqual(report["freshness"], "market_inactive_latest_session_snapshot")
        self.assertTrue(report["snapshot_valid"])

    def test_cached_calendar_cannot_prove_holiday_or_realtime(self):
        now = datetime(2026, 10, 3, 10, 0, tzinfo=SHANGHAI)
        client = Client()
        report = self.verify(client, now)
        self.assertIsNone(report["is_trading_day"])
        self.assertEqual(report["session_status"], "unknown")
        self.assertEqual(report["freshness"], "unverified")
        self.assertEqual(report["latest_market_date"], "2026-09-30")

    def test_calendar_accepts_only_documented_shanghai_midnight_date_identity(self):
        for offset in (0,):
            client = Client()
            for row in client.values[CALENDAR_PATH]["item"]:
                row["date_ms"] += offset
            report = self.verify(client)
            self.assertIs(report["is_trading_day"], True)
            self.assertEqual(report["latest_market_date"], "2026-09-30")
        for offset in (1, 3600 * 1000, 8 * 3600 * 1000, 9 * 3600 * 1000, 24 * 3600 * 1000):
            client = Client()
            client.values[CALENDAR_PATH]["item"][0]["date_ms"] += offset
            report = self.verify(client)
            self.assertIsNone(report["is_trading_day"])
            self.assertEqual(report["freshness"], "unverified")

    def test_live_calendar_sample_uses_documented_shanghai_midnight(self):
        now = datetime(2026, 10, 4, 7, 40, tzinfo=SHANGHAI)
        client = Client(fixtures(now))
        client.values[CALENDAR_PATH] = {"timestamp": 1791070416557, "item": [
            {"date_ms": 1759939200000, "date": "20251009"},
            {"date_ms": 1760025600000, "date": "20251010"},
            {"date_ms": 1790524800000, "date": "20260928"},
            {"date_ms": 1790611200000, "date": "20260929"},
            {"date_ms": 1790697600000, "date": "20260930"}]}
        report = self.verify(client, now)
        self.assertEqual(report["latest_market_date"], "2026-09-30")
        self.assertFalse(report["is_trading_day"])
        self.assertEqual(report["session_status"], "closed")

    def test_calendar_failure_does_not_invalidate_auth_and_price(self):
        for failure in (DataError(SECRET), ApiError(2003, SECRET), ApiError(3002, SECRET)):
            client = Client()
            client.values[CALENDAR_PATH] = failure
            report = self.verify(client)
            self.assertEqual(report["status"], "connected")
            self.assertTrue(report["auth_verified"])
            self.assertTrue(report["snapshot_valid"])
            self.assertEqual(report["freshness"], "unverified")
            self.assertIsNone(report["latest_market_date"])

    def test_invalid_calendar_rows_are_not_used_for_market_state(self):
        valid = fixtures()[CALENDAR_PATH]
        rows = valid["item"]
        cases = [None, {}, dict(valid, item=[]), dict(valid, item=rows[::-1]),
                 dict(valid, item=rows * 2), dict(valid, timestamp=ms(NOW) + 2001),
                 dict(valid, item=[dict(rows[0], date="20260930")]),
                 dict(valid, item=[dict(rows[0], date_ms=rows[0]["date_ms"] + 1)])]
        for value in cases:
            with self.subTest(value=value):
                client = Client()
                client.values[CALENDAR_PATH] = value
                report = self.verify(client)
                self.assertEqual(report["freshness"], "unverified")
                self.assertIsNone(report["is_trading_day"])

    def test_provenance_is_bounded_and_allowlisted(self):
        client = Client()
        client.audit = [{"path": SEARCH_PATH, "code": 0, "secret": SECRET}] * 30
        client.audit.extend([{"path": [SECRET]}, {"path": SECRET}, {"path": SEARCH_PATH, "code": SECRET}])
        report = self.verify(client)
        self.assertLessEqual(len(report["provenance"]["requests"]), 6)
        for record in report["provenance"]["requests"]:
            self.assertFalse(set(record) - {"path", "code", "attempt", "http_status", "network_error"})


if __name__ == "__main__":
    unittest.main()
