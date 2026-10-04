"""Read-only Hithink REST adapter. No broker endpoints or synthetic fallback.

``get`` returns the successful envelope's ``data`` dictionary. The only built-in
host is the one in the supplied API contract. Credentials never enter snapshots.
The adapter intentionally leaves unsupported execution facts unknown.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
BASE_URL = "https://fuyao.aicubes.cn"
KEY_ENV = "HITHINK_FINANCE_API_KEY"
SYMBOL = re.compile(r"\d{6}\.(SH|SZ|BJ)\Z")
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class DataError(RuntimeError):
    """Safe public error; raw responses, request headers and secrets omitted."""


class CredentialError(DataError):
    pass


class ApiError(DataError):
    def __init__(self, code: int | None, request_id: str | None = None):
        self.code = code
        self.request_id = request_id
        super().__init__(f"Hithink API failed (code={code}, request_id={request_id or 'unavailable'}).")


def _credential_path() -> Path:
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "hithink-finance" / "credentials.env"


def _valid_key(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if any(ord(char) < 33 or ord(char) > 126 for char in value) or '"' in value or "'" in value:
        raise CredentialError("API key has an invalid format; it must be an unquoted ASCII token.")
    return value


def _credentials() -> tuple[str | None, str | None]:
    key = _valid_key(os.environ.get(KEY_ENV))
    if key:
        return key, "environment:HITHINK_FINANCE_API_KEY"
    path = _credential_path()
    try:
        with path.open(encoding="utf-8") as handle:
            content = handle.read(65537)
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeError):
        raise CredentialError("Unable to read the Hithink user credential file.") from None
    if len(content) > 65536:
        raise CredentialError("The Hithink user credential file is too large.")
    for line in content.splitlines():
        name, separator, value = line.strip().partition("=")
        if separator and name.strip() == KEY_ENV:
            key = _valid_key(value)
            return key, "user_credential_file" if key else None
    return None, None


def credential_status() -> dict:
    """Only presence/source, never the key, its length, prefix or suffix."""
    try:
        key, source = _credentials()
        return {"present": bool(key), "source": source}
    except CredentialError as exc:
        return {"present": False, "source": None, "error": str(exc)}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward X-api-key to a redirected host, even on a 307/308.
        return None


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


def _request_id(value: Any, secret: str) -> str | None:
    # Only IDs shaped like documented UUID/hex identifiers are safe to report.
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F-]{16,64}", value) and secret not in value:
        return value
    return None


class HithinkClient:
    def __init__(self, api_key: str | None = None, *, timeout: float = 15,
                 max_retries: int = 2, opener=None, sleep=time.sleep):
        """At most 3 retries (4 total attempts); default 2 retries.

        ``opener`` / ``sleep`` are offline test seams. Production uses a fresh
        no-redirect urllib opener. No arbitrary host override is accepted.
        """
        if type(max_retries) is not int or not 0 <= max_retries <= 3:
            raise ValueError("max_retries must be between 0 and 3")
        if not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("timeout must be finite and between 0 and 120 seconds")
        self._api_key = _valid_key(api_key) if api_key is not None else _credentials()[0]
        if not self._api_key:
            raise CredentialError("Configure HITHINK_FINANCE_API_KEY or the Hithink user credential file first.")
        self.timeout = timeout
        self.max_retries = max_retries
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self._sleep = sleep
        self.audit: list[dict] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET a relative /api/ path and return data after strict envelope validation."""
        if not isinstance(path, str) or not re.fullmatch(r"/api/[a-z0-9/-]+", path) or "//" in path:
            raise DataError("Only relative documented /api/ paths are allowed.")
        params = params or {}
        if not isinstance(params, dict) or any(not isinstance(k, str) for k in params):
            raise DataError("Query parameters must be a dictionary.")
        if any("key" in key.lower() or "token" in key.lower() for key in params):
            raise DataError("Credentials must never be query parameters.")
        query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
        url = BASE_URL + path + ("?" + query if query else "")
        for attempt in range(self.max_retries + 1):
            retryable = False
            failure: DataError
            try:
                request = urllib.request.Request(url, headers={"X-api-key": self._api_key,
                    "Accept": "application/json", "User-Agent": "dragon-agent/1"}, method="GET")
                with self._opener.open(request, timeout=self.timeout) as response:
                    status = response.getcode()
                    if status != 200:
                        raise urllib.error.HTTPError(url, status, "HTTP failure", {}, None)
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise DataError("Hithink response exceeded the permitted size.")
                try:
                    envelope = json.loads(raw, parse_constant=_reject_constant)
                except (ValueError, UnicodeError):
                    raise DataError("Hithink returned invalid JSON.") from None
                if not isinstance(envelope, dict) or type(envelope.get("code")) is not int:
                    raise DataError("Hithink response is missing a valid business status code.")
                code = envelope["code"]
                request_id = _request_id(envelope.get("request_id"), self._api_key)
                self.audit.append({"path": path, "attempt": attempt + 1, "code": code, "request_id": request_id})
                if code == 0:
                    if not isinstance(envelope.get("data"), dict):
                        raise DataError("Successful Hithink response has no data object.")
                    return envelope["data"]
                failure = ApiError(code, request_id)
                retryable = code == 4001 or 5000 <= code <= 5999
            except urllib.error.HTTPError as exc:
                # Do not stringify HTTPError: URLs/headers/body can contain keys.
                status = exc.code
                exc.close()
                failure = DataError(f"Hithink HTTP request failed (status={status}).")
                retryable = status == 429 or 500 <= status <= 599
                self.audit.append({"path": path, "attempt": attempt + 1, "http_status": status})
            except (urllib.error.URLError, TimeoutError, OSError):
                failure = DataError("Hithink network request failed.")
                retryable = True
                self.audit.append({"path": path, "attempt": attempt + 1, "network_error": True})
            if not retryable or attempt == self.max_retries:
                raise failure from None
            self._sleep(min(0.5 * (2 ** attempt), 4.0))
        raise DataError("Hithink request failed.")  # Defensive; the loop always returns/raises.


def _date(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise DataError("trading_date must have YYYY-MM-DD format.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise DataError("trading_date is invalid.") from None


def _timestamp(value: Any) -> datetime | None:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, SHANGHAI)
    except (ValueError, OverflowError, OSError):
        return None


def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _number(value, *, integer=False):
    if type(value) not in (float, int) or not math.isfinite(value):
        return None
    if integer:
        return int(value) if value >= 0 and int(value) == value else None
    return value


def _bool(value):
    return value if type(value) is bool else None


def _pool(client, name: str, requested: date, *, page_size=200, max_pages=40) -> dict:
    """Read an explicitly dated pool without confusing availability with quote time.

    Production responses for old ``date_ms`` values have a timestamp from the
    current response assembly. Therefore ``timestamps`` describes only response
    metadata, never the market date or intraday freshness. No PIT availability
    is inferred for the requested historical session.
    """
    rows, timestamps, issues, warnings = [], [], [], []
    seen: set[str] = set()
    expected_total, expected_pages = None, None
    date_ms = int(datetime.combine(requested, dt_time(), SHANGHAI).timestamp() * 1000)
    for page in range(1, max_pages + 1):
        try:
            data = client.get(f"/api/a-share/special-data/{name}",
                              {"date_ms": date_ms, "page": page, "size": page_size})
        except DataError:
            issues.append(f"{name}:request_failed")
            break
        stamp = _timestamp(data.get("timestamp"))
        received = _now()
        if stamp is None:
            issues.append(f"{name}:missing_timestamp")
        elif stamp.date() < requested:
            issues.append(f"{name}:response_timestamp_before_requested_date")
        elif (stamp - received).total_seconds() > 2:
            issues.append(f"{name}:response_timestamp_after_capture")
        else:
            timestamps.append(stamp)
            if stamp > received:
                warnings.append(f"{name}:response_clock_skew_within_2_seconds")
        pagination, items = data.get("pagination"), data.get("item")
        if not isinstance(pagination, dict) or not isinstance(items, list):
            issues.append(f"{name}:invalid_shape")
            break
        total, pages = pagination.get("total"), pagination.get("pages")
        if (type(total) is not int or total < 0 or type(pages) is not int or pages < 0
                or type(pagination.get("page")) is not int or pagination["page"] != page
                or type(pagination.get("size")) is not int or pagination["size"] != page_size):
            issues.append(f"{name}:invalid_pagination")
            break
        if pages != math.ceil(total / page_size) and not (total == 0 and pages == 1):
            issues.append(f"{name}:inconsistent_page_count")
            break
        if expected_total is None:
            expected_total, expected_pages = total, pages
        elif (total, pages) != (expected_total, expected_pages):
            issues.append(f"{name}:pagination_changed_during_capture")
            break
        expected_length = max(0, min(page_size, total - (page - 1) * page_size))
        if len(items) != expected_length:
            issues.append(f"{name}:page_length_mismatch")
        for row in items:
            symbol = row.get("thscode") if isinstance(row, dict) else None
            if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
                issues.append(f"{name}:invalid_symbol")
            elif symbol in seen:
                issues.append(f"{name}:duplicate_symbol")
            else:
                seen.add(symbol)
                rows.append(row)
        if page >= pages:
            break
    else:
        issues.append(f"{name}:page_limit_exceeded")
    if expected_total is None or len(rows) != expected_total:
        issues.append(f"{name}:incomplete_rows")
    issues = list(dict.fromkeys(issues))
    return {"rows": rows, "complete": not issues, "reported_total": expected_total,
            "count": len(rows) if not issues else None, "timestamps": timestamps, "issues": issues,
            "warnings": list(dict.fromkeys(warnings)),
            "requested_trading_date": requested.isoformat(), "date_identity_source": "request_date_ms",
            "timestamp_semantics": "response_availability_not_market_freshness",
            "market_timestamp_verified": False, "available_at": _now().isoformat(),
            "pagination_consistency": "row_count_and_identity_only_no_version_token"}


def _calendar(client, requested: date) -> tuple[bool | None, list[str]]:
    try:
        data = client.get("/api/a-share/calendar/trading-days")
    except DataError:
        return None, ["calendar:request_failed"]
    if not isinstance(data.get("item"), list) or _timestamp(data.get("timestamp")) is None:
        return None, ["calendar:invalid_shape_or_timestamp"]
    dates = []
    for row in data["item"]:
        if not isinstance(row, dict):
            return None, ["calendar:invalid_row"]
        stamp = _timestamp(row.get("date_ms"))
        if stamp is None or row.get("date") != stamp.strftime("%Y%m%d") or stamp.time() != dt_time():
            return None, ["calendar:date_mismatch"]
        dates.append(stamp.date())
    if dates != sorted(set(dates)):
        return None, ["calendar:duplicate_or_unsorted_dates"]
    if requested in dates:
        return True, []
    # An absent date outside the observed window is unknown, never assumed closed.
    if dates and dates[0] <= requested <= dates[-1]:
        return False, ["calendar:requested_date_is_not_a_trading_day"]
    return None, ["calendar:requested_date_not_covered"]


def collect_snapshot(client: HithinkClient, trading_date: str, max_symbols: int = 30) -> dict:
    """Collect complete pools and enrich at most 100 current-day symbols.

    Historical pool availability is capture time, never retroactively backdated.
    Pool ``is_new`` means unopened IPO only, not complete listing eligibility.
    No current quote is ever attached to a historical pool. All unsupported
    execution fields stay null, and ``execution_verified`` is always False.
    """
    requested = _date(trading_date)
    started = _now()
    if requested > started.date():
        raise DataError("A future trading date cannot be collected.")
    if type(max_symbols) is not int or not 1 <= max_symbols <= 100:
        raise DataError("max_symbols must be between 1 and 100.")
    trading_day_verified, calendar_issues = _calendar(client, requested)
    pools = {name: _pool(client, name, requested) for name in
             ("limit-up-pool", "limit-down-pool", "limit-break-pool")}
    coverage_issues = calendar_issues + [issue for pool in pools.values() for issue in pool["issues"]]
    issues = []
    up = pools["limit-up-pool"]
    selected = sorted(up["rows"], key=lambda row: (
        -(_number(row.get("continue_day_cnt"), integer=True) or 0),
        -(_number(row.get("seal_money")) or 0), row["thscode"]))[:max_symbols]
    stocks = []
    for row in selected:
        stocks.append({"symbol": row["thscode"], "name": row.get("name"),
            "price": _number(row.get("last_price")), "prev_close": None,
            "change_pct": _number(row.get("price_change_ratio_pct")), "turnover": None,
            "turnover_pct": None, "board_count": _number(row.get("continue_day_cnt"), integer=True),
            "seal_money": _number(row.get("seal_money")), "max_seal_money": _number(row.get("max_seal_money")),
            "theme": row.get("limit_up_reason") or None, "first_limit_time": row.get("limit_up_time"),
            "is_st": _bool(row.get("is_st")), "is_new": _bool(row.get("is_new")),
            "suspended": None, "quote_asof": None, "limit_up": None, "limit_down": None,
            "execution_verified": False, "batch_quote_asof": None,
            "open": None, "high": None, "low": None, "volume": None})
    # Pool timestamps are response-assembly metadata in observed production
    # responses. Only the documented current quote batch can supply asof here.
    source_times = []
    if stocks and requested == started.date():
        try:
            quote_data = client.get("/api/a-share/prices/snapshot", {"thscodes": ",".join(s["symbol"] for s in stocks)})
            stamp = _timestamp(quote_data.get("timestamp"))
            if stamp is None or stamp.date() != requested:
                issues.append("quotes:missing_timestamp_or_date_mismatch;enrichment_rejected")
            elif not isinstance(quote_data.get("item"), list):
                issues.append("quotes:invalid_shape;enrichment_rejected")
            else:
                quotes = {}
                selected_symbols = {stock["symbol"] for stock in stocks}
                quote_shape_ok = True
                for row in quote_data["item"]:
                    symbol = row.get("thscode") if isinstance(row, dict) else None
                    if symbol not in selected_symbols or symbol in quotes:
                        quote_shape_ok = False
                        break
                    quotes[symbol] = row
                if not quote_shape_ok:
                    issues.append("quotes:unexpected_or_duplicate_symbol;enrichment_rejected")
                else:
                    source_times.append(stamp)
                    if len(quotes) != len(stocks):
                        issues.append("quotes:some_symbols_missing")
                    fields = {"price": "last_price", "prev_close": "prev_price",
                        "change_pct": "price_change_ratio_pct", "turnover": "turnover",
                        "open": "open_price", "high": "high_price", "low": "low_price", "volume": "volume"}
                    for stock in stocks:
                        if stock["symbol"] in quotes:
                            row = quotes[stock["symbol"]]
                            stock.update({target: _number(row.get(source)) for target, source in fields.items()})
                            stock["batch_quote_asof"] = stamp.isoformat()
        except DataError:
            issues.append("quotes:request_failed;pool_values_only")
    elif stocks:
        issues.append("quotes:historical_enrichment_unavailable")
    captured = _now()
    asof = max(source_times) if source_times else captured
    # Even a fresh quote batch does not prove that all pool contents are fresh.
    # Preserve full market counts for research while failing closed for timing.
    coverage_issues.append("pools:market_timestamp_unverified")
    if not source_times:
        coverage_issues.append("missing_verified_market_timestamp")
    if asof > captured:
        coverage_issues.append("upstream_timestamp_after_capture")
        # Reject impossible timing without forging the actual capture timestamp.
        raise DataError("Upstream data timestamp is later than the capture clock.")
    boards = [_number(row.get("continue_day_cnt"), integer=True) for row in up["rows"]]
    max_height = max(boards, default=0) if up["complete"] and all(v is not None for v in boards) else None
    issues.extend(["quotes:per_symbol_timestamp_unavailable", "execution:suspension_and_price_limits_unavailable",
                   "execution:pool_is_new_is_unopened_IPO_only", "theme:limit_up_reason_is_proxy"])
    return {"schema_version": 1, "source": "hithink", "trading_date": trading_date,
        "asof": asof.isoformat(), "available_at": captured.isoformat(),
        "coverage": {"complete": not coverage_issues, "issues": list(dict.fromkeys(coverage_issues))},
        "market": {"limit_up_count": up["count"], "limit_down_count": pools["limit-down-pool"]["count"],
                   "broken_count": pools["limit-break-pool"]["count"], "max_height": max_height},
        "stocks": stocks,
        "provenance": {"adapter": "hithink-rest-v1", "base_url": BASE_URL,
            "capture_started_at": started.isoformat(), "captured_at": captured.isoformat(),
            "availability_policy": "capture_time_no_historical_backdating",
            "asof_basis": "latest_valid_quote_batch_timestamp" if source_times else "capture_clock_not_market_timestamp",
            "trading_day_verified": trading_day_verified,
            "source_timestamps": {},
            "pool_response_timestamps": {name: [stamp.isoformat() for stamp in pool["timestamps"]] for name, pool in pools.items()},
            "pool_timestamp_semantics": "response_availability_not_market_freshness",
            "pool_date_identity_source": "request_date_ms",
            "pool_requested_trading_date": trading_date,
            "pool_market_timestamp_verified": False,
            "pool_coverage_complete": all(pool["complete"] for pool in pools.values()),
            "pool_captured_at": {name: pool["available_at"] for name, pool in pools.items()},
            "pool_metadata_warnings": [warning for pool in pools.values() for warning in pool["warnings"]],
            "pool_reported_totals": {name: pool["reported_total"] for name, pool in pools.items()},
            "pool_selected": "limit-up-pool", "pool_rows": len(up["rows"]), "selected_symbols": len(stocks),
            "selection_order": "board_count_desc,seal_money_desc,symbol_asc",
            "units": {"money": "CNY", "volume": "shares", "percentage": "percentage_points"},
            "issues": issues, "requests": list(getattr(client, "audit", []))}}


def _aware_iso(value: Any) -> datetime:
    if not isinstance(value, str):
        raise DataError("Snapshot timestamps must be timezone-aware ISO strings.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise DataError("Snapshot timestamp format is invalid.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DataError("Snapshot timestamps must include a timezone.")
    return parsed


def load_snapshot(path) -> dict:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            value = json.load(handle, parse_constant=_reject_constant)
    except (OSError, UnicodeError, ValueError):
        raise DataError("Unable to read a valid JSON snapshot.") from None
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise DataError("Unsupported snapshot schema_version.")
    if value.get("source") not in {"demo", "hithink", "imported"}:
        raise DataError("Invalid snapshot source.")
    _date(value.get("trading_date"))
    if _aware_iso(value.get("available_at")) < _aware_iso(value.get("asof")):
        raise DataError("Snapshot available_at cannot precede asof.")
    coverage = value.get("coverage")
    if (not isinstance(coverage, dict) or type(coverage.get("complete")) is not bool
            or not isinstance(coverage.get("issues"), list) or not isinstance(value.get("market"), dict)
            or not isinstance(value.get("stocks"), list)):
        raise DataError("Invalid snapshot coverage, market or stocks.")
    seen = set()
    for row in value["stocks"]:
        symbol = row.get("symbol") if isinstance(row, dict) else None
        if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol) or symbol in seen:
            raise DataError("Snapshot has invalid or duplicate symbols.")
        seen.add(symbol)
        for key in ("is_st", "is_new", "suspended", "execution_verified"):
            if row.get(key) is not None and type(row[key]) is not bool:
                raise DataError("Snapshot boolean fields must be true, false or null.")
        for key in ("price", "prev_close", "change_pct", "turnover", "turnover_pct", "board_count",
                    "seal_money", "max_seal_money", "limit_up", "limit_down", "open", "high", "low", "volume"):
            if row.get(key) is not None and _number(row[key]) is None:
                raise DataError("Snapshot numerical fields must be finite numbers or null.")
        if row.get("quote_asof") is not None:
            _aware_iso(row["quote_asof"])
    return value


def save_json(path, value) -> None:
    """Atomically save UTF-8 JSON, rejecting NaN/Infinity; create only parent dirs."""
    target = Path(path)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
