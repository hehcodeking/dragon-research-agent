"""Offline contract/security tests using documented ApiResponse shapes."""
import copy
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from dragon_agent.data import (ApiError, CredentialError, DataError, HithinkClient,
    SHANGHAI, _NoRedirect, collect_snapshot, credential_status, load_snapshot, save_json)

CAPTURE = datetime(2026, 9, 30, 15, 1, tzinfo=SHANGHAI)
DATA_TIME = CAPTURE - timedelta(minutes=1)
MS = int(DATA_TIME.timestamp() * 1000)
SECRET = "test-secret-never-print"


class Response:
    def __init__(self, value, status=200):
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def getcode(self):
        return self.status

    def read(self, size):
        return self.raw[:size]


class Opener:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return Response(result)


def envelope(data, code=0, **kwargs):
    return {"code": code, "message": "success", "request_id": "a1b2c3d4e5f6789012345678abcdef01",
            "data": data, **kwargs}


def stock(index=1):
    return {"thscode": f"{600000 + index:06d}.SH", "ticker": f"{600000 + index:06d}",
        "name": f"测试{index}", "is_st": False, "is_new": False, "last_price": 11.0,
        "price_change_ratio_pct": 10.0, "limit_up_time": "09:34", "limit_up_reason": "存储芯片",
        "continue_day_cnt": 2, "seal_money": 123456789.12, "max_seal_money": 234567890.12}


class MarketClient:
    def __init__(self, rows=None, requested="2026-09-30"):
        self.rows = rows if rows is not None else [stock()]
        self.requested = requested
        self.calls = []
        self.audit = []
        self.transform = lambda path, data: data

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path.endswith("/trading-days"):
            date_ms = int(datetime.fromisoformat(self.requested).replace(tzinfo=SHANGHAI).timestamp() * 1000)
            data = {"timestamp": MS, "item": [{"date": self.requested.replace("-", ""), "date_ms": date_ms}]}
        elif path.endswith("/snapshot"):
            data = {"timestamp": MS, "total": len(params["thscodes"].split(",")), "item": [
                {"thscode": symbol, "last_price": 11.0, "prev_price": 10.0, "price_change_ratio_pct": 10.0,
                 "turnover": 3937375200, "volume": 3098875, "open_price": 10.5,
                 "high_price": 11.0, "low_price": 10.2} for symbol in params["thscodes"].split(",")]}
        else:
            rows = self.rows if path.endswith("limit-up-pool") else []
            size, page = params["size"], params["page"]
            data = {"timestamp": MS, "pagination": {"total": len(rows), "pages": (len(rows) + size - 1) // size,
                "size": size, "page": page}, "item": copy.deepcopy(rows[(page - 1) * size:page * size])}
        return self.transform(path, data)


class ClientTests(unittest.TestCase):
    def client(self, opener, retries=2):
        return HithinkClient(SECRET, opener=opener, sleep=lambda seconds: None, max_retries=retries)

    def test_success_requires_code_zero_and_returns_data(self):
        opener = Opener(envelope({"item": []}))
        client = self.client(opener)
        self.assertEqual(client.get("/api/a-share/prices/snapshot", {"thscodes": "600519.SH"}), {"item": []})
        request = opener.requests[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("X-api-key"), SECRET)
        self.assertNotIn(SECRET, request.full_url)
        self.assertNotIn(SECRET, json.dumps(client.audit))

    def test_http_200_business_error_never_becomes_empty_success(self):
        for code in (1001, 2001, 2003, 3002):
            with self.subTest(code=code):
                opener = Opener(envelope(None, code=code, message=SECRET))
                with self.assertRaises(ApiError) as caught:
                    self.client(opener).get("/api/a-share/prices/snapshot")
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertEqual(len(opener.requests), 1)

    def test_missing_or_wrong_status_code_and_null_success_rejected(self):
        for response in ({"data": {}}, envelope({}, code=True), envelope({}, code="0"), envelope(None), b"not json"):
            with self.subTest(response=response), self.assertRaises(DataError):
                self.client(Opener(response)).get("/api/a-share/prices/snapshot")

    def test_transient_business_failures_retry_with_hard_bound(self):
        opener = Opener(*(envelope(None, code=4001) for _ in range(4)))
        with self.assertRaises(ApiError):
            self.client(opener, retries=3).get("/api/a-share/prices/snapshot")
        self.assertEqual(len(opener.requests), 4)
        with self.assertRaises(ValueError):
            HithinkClient(SECRET, max_retries=4)

    def test_network_retry_recovers_without_logging_exception_secret(self):
        opener = Opener(urllib.error.URLError(SECRET), envelope({"item": []}))
        client = self.client(opener)
        self.assertEqual(client.get("/api/a-share/prices/snapshot"), {"item": []})
        self.assertNotIn(SECRET, json.dumps(client.audit))

    def test_http_error_message_and_redirect_target_do_not_leak(self):
        error = urllib.error.HTTPError("https://evil.test/?key=" + SECRET, 302, SECRET, {"X-api-key": SECRET}, io.BytesIO(SECRET.encode()))
        opener = Opener(error)
        with self.assertRaises(DataError) as caught:
            self.client(opener).get("/api/a-share/prices/snapshot")
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(len(opener.requests), 1)
        request = urllib.request.Request("https://fuyao.aicubes.cn/api/a-share/prices/snapshot", headers={"X-api-key": SECRET})
        self.assertIsNone(_NoRedirect().redirect_request(request, None, 302, "redirect", {}, "https://evil.test"))
        self.assertTrue(any(isinstance(h, _NoRedirect) for h in HithinkClient(SECRET)._opener.handlers))

    def test_absolute_urls_and_query_credentials_rejected_before_io(self):
        opener = Opener()
        client = self.client(opener)
        for path in ("https://evil.test", "//evil.test", "/api/../secret", "/api/x?key=value"):
            with self.assertRaises(DataError):
                client.get(path)
        with self.assertRaises(DataError):
            client.get("/api/a-share/prices/snapshot", {"api_key": SECRET})
        self.assertEqual(opener.requests, [])

    def test_request_id_cannot_echo_secret(self):
        opener = Opener(envelope(None, code=2003, request_id=SECRET))
        with self.assertRaises(ApiError) as caught:
            self.client(opener).get("/api/a-share/prices/snapshot")
        self.assertIsNone(caught.exception.request_id)

    def test_nonfinite_response_is_not_accepted(self):
        with self.assertRaises(DataError):
            self.client(Opener(b'{"code":0,"data":{"x":NaN}}')).get("/api/a-share/prices/snapshot")


class CredentialTests(unittest.TestCase):
    def test_environment_wins_status_does_not_expose_key(self):
        with patch.dict("os.environ", {"HITHINK_FINANCE_API_KEY": SECRET}, clear=True), patch("dragon_agent.data._credential_path") as path:
            result = credential_status()
        self.assertEqual(result, {"present": True, "source": "environment:HITHINK_FINANCE_API_KEY"})
        path.assert_not_called()
        self.assertNotIn(SECRET, json.dumps(result))

    def test_user_file_read_without_shell_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.env"
            path.write_text("# ignored\nHITHINK_FINANCE_API_KEY=" + SECRET + "\n", encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True), patch("dragon_agent.data._credential_path", return_value=path):
                self.assertEqual(credential_status(), {"present": True, "source": "user_credential_file"})

    def test_generic_api_key_is_not_sent_to_unrelated_service(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"API_KEY": SECRET}, clear=True), patch("dragon_agent.data._credential_path", return_value=Path(directory) / "missing"):
                self.assertFalse(credential_status()["present"])
                with self.assertRaises(CredentialError):
                    HithinkClient()

    def test_header_injection_and_quoted_keys_rejected_without_echo(self):
        for bad in (SECRET + "\r\nHeader:value", '"' + SECRET + '"'):
            with self.subTest(value=bad), self.assertRaises(CredentialError) as caught:
                HithinkClient(bad)
            self.assertNotIn(SECRET, str(caught.exception))


class SnapshotTests(unittest.TestCase):
    def collect(self, client=None, date="2026-09-30", cap=30):
        with patch("dragon_agent.data._now", return_value=CAPTURE):
            return collect_snapshot(client or MarketClient(), date, cap)

    def test_units_nulls_and_execution_uncertainty_preserved(self):
        client = MarketClient()
        client.rows[0]["is_st"] = None
        client.rows[0]["seal_money"] = None
        result = self.collect(client)
        row = result["stocks"][0]
        self.assertEqual(row["change_pct"], 10.0)
        self.assertEqual(row["turnover"], 3937375200)
        self.assertEqual(row["volume"], 3098875)
        self.assertIsNone(row["is_st"])
        self.assertIsNone(row["seal_money"])
        self.assertIsNone(row["quote_asof"])
        self.assertIsNone(row["limit_up"])
        self.assertIsNone(row["suspended"])
        self.assertFalse(row["execution_verified"])
        self.assertEqual(row["batch_quote_asof"], DATA_TIME.isoformat())
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(result["provenance"]["pool_coverage_complete"])
        self.assertIn("pools:market_timestamp_unverified", result["coverage"]["issues"])

    def test_full_market_count_not_capped_by_quote_budget(self):
        client = MarketClient([stock(i) for i in range(1, 251)])
        result = self.collect(client, cap=5)
        self.assertEqual(result["market"]["limit_up_count"], 250)
        self.assertEqual(len(result["stocks"]), 5)
        pool_calls = [params for path, params in client.calls if path.endswith("limit-up-pool")]
        self.assertEqual([c["page"] for c in pool_calls], [1, 2])
        quote_call = [params for path, params in client.calls if path.endswith("/snapshot")][0]
        self.assertEqual(set(quote_call), {"thscodes"})
        self.assertEqual(len(quote_call["thscodes"].split(",")), 5)
        self.assertEqual(client.calls[0][1], None)

    def test_historical_capture_never_fetches_todays_quotes_or_backdates_availability(self):
        client = MarketClient(requested="2026-09-29")
        historical_ms = int(datetime(2026, 9, 29, 15, tzinfo=SHANGHAI).timestamp() * 1000)
        client.transform = lambda path, data: {**data, "timestamp": historical_ms}
        result = self.collect(client, date="2026-09-29")
        self.assertFalse(any(path.endswith("/snapshot") for path, _ in client.calls))
        self.assertEqual(result["available_at"], CAPTURE.isoformat())
        self.assertEqual(result["asof"], CAPTURE.isoformat())
        self.assertEqual(result["provenance"]["asof_basis"], "capture_clock_not_market_timestamp")
        self.assertIsNone(result["stocks"][0]["prev_close"])

    def test_pool_date_mismatch_invalidates_counts(self):
        client = MarketClient()
        prior_ms = MS - 24 * 60 * 60 * 1000
        client.transform = lambda path, data: {**data, "timestamp": prior_ms} if path.endswith("limit-up-pool") else data
        result = self.collect(client)
        self.assertFalse(result["coverage"]["complete"])
        self.assertIsNone(result["market"]["limit_up_count"])
        self.assertIn("limit-up-pool:response_timestamp_before_requested_date", result["coverage"]["issues"])

    def test_null_pool_timestamp_is_not_treated_as_zero_stocks(self):
        client = MarketClient([])
        client.transform = lambda path, data: {**data, "timestamp": None} if path.endswith("limit-down-pool") else data
        result = self.collect(client)
        self.assertIsNone(result["market"]["limit_down_count"])
        self.assertFalse(result["coverage"]["complete"])

    def test_duplicate_pool_symbols_cannot_inflate_market(self):
        client = MarketClient([stock(), stock()])
        result = self.collect(client)
        self.assertFalse(result["coverage"]["complete"])
        self.assertIsNone(result["market"]["limit_up_count"])
        self.assertEqual(len(result["stocks"]), 1)

    def test_pagination_drift_or_truncation_invalidates_coverage(self):
        for corruption in ("total", "short"):
            client = MarketClient([stock(i) for i in range(1, 251)])
            def transform(path, data):
                if path.endswith("limit-up-pool") and data["pagination"]["page"] == 2:
                    if corruption == "total":
                        data["pagination"]["total"] += 1
                    elif corruption == "short":
                        data["item"].pop()
                    else:
                        data["timestamp"] += 1000
                return data
            client.transform = transform
            with self.subTest(corruption=corruption):
                result = self.collect(client)
                self.assertFalse(result["coverage"]["complete"])
                self.assertIsNone(result["market"]["limit_up_count"])

    def test_quote_date_mismatch_rejects_enrichment_not_pool_counts(self):
        client = MarketClient()
        client.transform = lambda path, data: {**data, "timestamp": MS - 86400000} if path.endswith("/snapshot") else data
        result = self.collect(client)
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(result["provenance"]["pool_coverage_complete"])
        self.assertIn("pools:market_timestamp_unverified", result["coverage"]["issues"])
        self.assertIsNone(result["stocks"][0]["prev_close"])
        self.assertIsNone(result["stocks"][0]["batch_quote_asof"])

    def test_future_upstream_timestamp_is_rejected(self):
        client = MarketClient()
        client.transform = lambda path, data: {**data, "timestamp": MS + 3600000}
        with self.assertRaises(DataError):
            self.collect(client)

    def test_calendar_date_disagreement_is_unknown(self):
        client = MarketClient()
        def transform(path, data):
            if path.endswith("/trading-days"):
                data["item"][0]["date"] = "20260929"
            return data
        client.transform = transform
        result = self.collect(client)
        self.assertIsNone(result["provenance"]["trading_day_verified"])
        self.assertFalse(result["coverage"]["complete"])

    def test_missing_board_count_does_not_become_known_zero(self):
        client = MarketClient()
        client.rows[0]["continue_day_cnt"] = None
        self.assertIsNone(self.collect(client)["market"]["max_height"])

    def test_invalid_date_or_budget_fails_before_network(self):
        client = MarketClient()
        for day, cap in (("2026-10-01", 30), ("2026-02-30", 30), ("20260930", 30), ("2026-09-30", 101)):
            with self.subTest(day=day, cap=cap), self.assertRaises(DataError):
                self.collect(client, day, cap)
        self.assertFalse(client.calls)

    def test_save_load_round_trip_and_invalid_availability(self):
        value = self.collect()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "snapshot.json"
            save_json(path, value)
            self.assertEqual(load_snapshot(path), value)
            value["available_at"] = "2026-09-30T14:00:00+08:00"
            save_json(path, value)
            with self.assertRaises(DataError):
                load_snapshot(path)

    def test_load_rejects_nan_and_duplicate_symbols(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text('{"schema_version":1,"x":NaN}')
            with self.assertRaises(DataError):
                load_snapshot(path)
            value = self.collect()
            value["stocks"].append(copy.deepcopy(value["stocks"][0]))
            save_json(path, value)
            with self.assertRaises(DataError):
                load_snapshot(path)


if __name__ == "__main__":
    unittest.main()
