"""Small read-only connection diagnostic; never substitutes demonstration data.

Successful HithinkClient.get calls have already passed HTTP and business-code
validation. Quote timestamps describe the provider snapshot, not an individually
verified exchange tick. At most three logical GETs are made; the default client
has one bounded retry per request and an eight-second timeout.
"""
from .data import (ApiError, CredentialError, DataError, HithinkClient, SYMBOL,
                   _now, _number, _timestamp)
from .realtime_data import ASSEMBLY_CLOCK_SKEW_SECONDS, _validated_calendar, session_status


SEARCH_PATH = "/api/meta/tickers/search"
QUOTE_PATH = "/api/a-share/prices/snapshot"
CALENDAR_PATH = "/api/a-share/calendar/trading-days"
PATHS = {SEARCH_PATH, QUOTE_PATH, CALENDAR_PATH}
FRESH_SNAPSHOT_SECONDS = 60


def _base():
    return {
        "provider": "同花顺金融数据服务（hithink finance）",
        "source": "hithink",
        "checked_at": _now().isoformat(),
        "status": "not_checked",
        "connected": False,
        "auth_verified": False,
        "data_ready": False,
        "snapshot_valid": False,
        "symbol": None,
        "price": None,
        "currency": "CNY",
        "quote_asof": None,
        "quote_age_seconds": None,
        "latest_market_date": None,
        "is_trading_day": None,
        "session_status": "unknown",
        "freshness": "unverified",
        "freshness_threshold_seconds": FRESH_SNAPSHOT_SECONDS,
        "per_symbol_freshness_verified": False,
        "live_orders_sent": False,
        "issues": [],
        "next_step": None,
        "provenance": {
            "adapter": "hithink-connection-v1",
            "quote_timestamp_semantics": "latest_upstream_effective_time_in_snapshot",
            "per_symbol_timestamp_available": False,
            "requests": [],
        },
        "note": "连接诊断仅核验真实接口与快照；快照时间不证明逐股行情实时性，也不代表选股或交易验证。",
    }


def _finish(report, client=None):
    report["checked_at"] = _now().isoformat()
    # Never copy provider responses, exception text, headers, request IDs or
    # arbitrary audit objects into a user-facing report.
    audit = getattr(client, "audit", [])
    if isinstance(audit, list):
        safe = []
        for entry in audit[-6:]:
            if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                    or entry["path"] not in PATHS):
                continue
            item = {"path": entry["path"]}
            for field in ("attempt", "code", "http_status"):
                if type(entry.get(field)) is int:
                    item[field] = entry[field]
            if entry.get("network_error") is True:
                item["network_error"] = True
            safe.append(item)
        report["provenance"]["requests"] = safe
    return report


def _failure(report, exc, stage, client):
    report["failure_stage"] = stage
    if isinstance(exc, CredentialError):
        report["status"] = "credential_missing_or_invalid"
        report["next_step"] = "在本机运行 connect.command 或 ./run.command auth，安全保存有效 API Key 后重新验证。"
    elif isinstance(exc, ApiError):
        code = exc.code if type(exc.code) is int else None
        report["business_code"] = code
        if code in (2001, 2003):
            report["status"] = "auth_failed"
            report["next_step"] = "在官方 API Key 管理页检查 Key 与接口权限，更新本机凭据后重新验证。"
        elif code == 3002:
            report["status"] = "data_not_ready"
            report["next_step"] = "数据源尚未就绪；保留当前凭据，稍后重新验证真实行情。"
        elif code == 4001:
            report["status"] = "rate_limited"
            report["next_step"] = "降低请求频率，稍后重新验证连接。"
        else:
            report["status"] = "api_error"
            report["next_step"] = "检查数据服务状态与接口授权后重新验证。"
    else:
        audit = getattr(client, "audit", [])
        last = audit[-1] if isinstance(audit, list) and audit and isinstance(audit[-1], dict) else {}
        if last.get("network_error") is True:
            report["status"] = "network_error"
            report["next_step"] = "检查本机网络能否访问 fuyao.aicubes.cn，恢复后重新验证。"
        elif last.get("http_status") in (401, 403):
            report["status"] = "auth_failed"
            report["next_step"] = "在官方 API Key 管理页检查 Key 与接口权限后重新验证。"
        elif type(last.get("http_status")) is int:
            report["status"] = "http_error"
            report["http_status"] = last["http_status"]
            report["next_step"] = "检查数据服务与网络代理状态后重新验证。"
        else:
            report["status"] = "invalid_response"
            report["next_step"] = "接口响应未通过契约校验；检查服务状态后重新验证。"
    report["issues"].append(stage + ":" + report["status"])
    return _finish(report, client)


def _stamp(value, now, *, assembly=False):
    try:
        stamp = _timestamp(value) if type(value) is int else None
    except (ValueError, OverflowError, OSError):
        stamp = None
    tolerance = ASSEMBLY_CLOCK_SKEW_SECONDS if assembly else 0
    if stamp is None or (stamp - now).total_seconds() > tolerance or stamp.date() > now.date():
        raise DataError("Missing, invalid or future provider timestamp.")
    return stamp


def _calendar(data, now):
    dates, stamp = _validated_calendar(data, now)
    # The endpoint's advertised window ends today. A prior-day cached snapshot
    # cannot independently prove that today is closed, particularly on holidays.
    verified_today = stamp.date() == now.date()
    return dates[-1], (now.date() in dates if verified_today else None)


def verify_connection(client=None) -> dict:
    """Verify auth, a resolved symbol, one real price snapshot and calendar.

    ``status=connected`` and ``snapshot_valid=True`` can coexist with stale,
    closed-market or unverified freshness. No broker calls or demo fallback
    exist in this path. Inject a HithinkClient-compatible client for offline QA.
    """
    report = _base()
    try:
        client = client if client is not None else HithinkClient(timeout=8, max_retries=1)
    except CredentialError as exc:
        return _failure(report, exc, "credentials", None)

    try:
        data = client.get(SEARCH_PATH, {"q": "600519", "asset_type": "a-share", "limit": 1})
        report["auth_verified"] = True
        report["connected"] = True
        if not isinstance(data, dict):
            raise DataError("Invalid symbol search response.")
        _stamp(data.get("timestamp"), _now(), assembly=True)
        rows = data.get("item")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise DataError("Symbol search did not uniquely resolve the diagnostic ticker.")
        row = rows[0]
        symbol = row.get("thscode")
        if (not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol)
                or row.get("ticker") != "600519" or symbol.split(".")[0] != "600519"
                or row.get("asset_type") != "a-share"
                or row.get("exchange") != symbol.split(".")[1]
                or row.get("currency") != "CNY"):
            raise DataError("Invalid symbol search identity.")
        report["symbol"] = symbol
    except DataError as exc:
        return _failure(report, exc, "symbol_search", client)

    try:
        data = client.get(QUOTE_PATH, {"thscodes": symbol})
        if not isinstance(data, dict):
            raise DataError("Invalid quote response.")
        now = _now()
        stamp = _stamp(data.get("timestamp"), now)
        rows = data.get("item")
        # `total` is the market universe count, even for an explicit symbol.
        if (type(data.get("total")) is not int or data["total"] < 1
                or not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)):
            raise DataError("Invalid quote response shape.")
        row = rows[0]
        if row.get("thscode") != symbol or row.get("ticker") != "600519":
            raise DataError("Quote identity mismatch.")
        try:
            price = _number(row.get("last_price"))
        except (ValueError, OverflowError):
            price = None
        if price is None or price <= 0:
            raise DataError("Missing or invalid last price.")
        report.update(status="connected", data_ready=True, snapshot_valid=True,
                      price=price, quote_asof=stamp.isoformat(),
                      quote_age_seconds=round((now - stamp).total_seconds(), 3))
    except DataError as exc:
        return _failure(report, exc, "quote", client)

    try:
        data = client.get(CALENDAR_PATH)
        now = _now()
        latest, is_trading_day = _calendar(data, now)
        calendar_stamp = _timestamp(data.get("timestamp"))
        report["provenance"]["calendar_assembly_clock_skew_tolerance_seconds"] = ASSEMBLY_CLOCK_SKEW_SECONDS
        if calendar_stamp > now:
            report["issues"].append("calendar:assembly_clock_skew_within_2_seconds")
        report["latest_market_date"] = latest.isoformat()
        report["is_trading_day"] = is_trading_day
        report["session_status"] = session_status(now, is_trading_day)
        age = (now - stamp).total_seconds()
        report["quote_age_seconds"] = round(age, 3)
        if is_trading_day is None:
            report["issues"].append("calendar:today_not_verified")
            report["freshness"] = "unverified"
        elif is_trading_day is False and stamp.date() > latest:
            report["freshness"] = "unverified"
            report["issues"].append("quote:batch_timestamp_on_nontrading_date")
        elif stamp.date() != latest:
            report["freshness"] = "stale_snapshot"
            report["issues"].append("quote:not_latest_market_date")
        elif report["session_status"] in ("closed", "lunch", "preopen"):
            report["freshness"] = "market_inactive_latest_session_snapshot"
        elif age <= FRESH_SNAPSHOT_SECONDS:
            report["freshness"] = "recent_snapshot_per_symbol_unverified"
        else:
            report["freshness"] = "stale_snapshot"
            report["issues"].append("quote:older_than_freshness_threshold")
    except DataError as exc:
        report["issues"].append("calendar:unavailable_or_invalid")
        if isinstance(exc, ApiError) and exc.code in (2001, 2003):
            report["issues"].append("calendar:permission_denied")
        report["freshness"] = "unverified"
    report["next_step"] = ("连接及快照校验通过；交易时段运行真实行情工作台，继续核验逐股数据与竞价特征。"
                           if report["freshness"] == "recent_snapshot_per_symbol_unverified"
                           else "连接及快照校验通过；在交易时段重新验证数据时间与选股特征覆盖。")
    return _finish(report, client)
