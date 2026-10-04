"""Bounded current-day research collection with previous-session context cache.

No individual quote timestamps, suspension status or final reseal times are
invented. Auction turnover is never relabeled as intraday stock turnover.
"""
from datetime import datetime, time as dt_time
import time

from .data import (ApiError, DataError, SHANGHAI, SYMBOL, _bool, _date, _number, _pool,
                   _timestamp, _now)
from .auction_units import auction_volume_fields

ASSEMBLY_CLOCK_SKEW_SECONDS = 2


def session_status(now, is_trading_day):
    if is_trading_day is not True:
        return "closed" if is_trading_day is False else "unknown"
    second = now.hour * 3600 + now.minute * 60 + now.second
    if second < 555 * 60:
        return "preopen"
    if 555 * 60 <= second <= 565 * 60:
        return "auction"
    if 565 * 60 < second < 570 * 60:
        return "preopen"
    if 570 * 60 <= second <= 690 * 60 or 780 * 60 <= second <= 900 * 60:
        return "continuous"
    if 690 * 60 < second < 780 * 60:
        return "lunch"
    return "closed"


def _validated_calendar(data, now):
    """Validate calendar identity without treating date encoding as quote time.

    Dates must match the documented Shanghai midnight encoding. The assembly
    clock may lead this machine by at most two seconds within the same day;
    this tolerance never applies to individual quotes or trading decisions.
    """
    if not isinstance(data, dict):
        raise DataError("Calendar response is incomplete.")
    try:
        source = _timestamp(data.get("timestamp")) if type(data.get("timestamp")) is int else None
    except (ValueError, OverflowError, OSError):
        source = None
    rows = data.get("item")
    if (source is None or (source - now).total_seconds() > ASSEMBLY_CLOCK_SKEW_SECONDS
            or source.date() > now.date() or not isinstance(rows, list) or not 1 <= len(rows) <= 370):
        raise DataError("Calendar response is incomplete.")
    dates = []
    for row in rows:
        try:
            stamp = (_timestamp(row.get("date_ms")) if isinstance(row, dict)
                     and type(row.get("date_ms")) is int else None)
        except (ValueError, OverflowError, OSError):
            stamp = None
        if stamp is None:
            raise DataError("Calendar date identity mismatch.")
        if stamp.time() != dt_time() or row.get("date") != stamp.strftime("%Y%m%d"):
            raise DataError("Calendar date identity mismatch.")
        if stamp.date() > now.date() or stamp.date() > source.date():
            raise DataError("Calendar contains a future trading date.")
        dates.append(stamp.date())
    if dates != sorted(set(dates)):
        raise DataError("Calendar must contain unique ascending sessions.")
    return dates, source


def _calendar_context(client, requested):
    data = client.get("/api/a-share/calendar/trading-days")
    now = _now()
    dates, source = _validated_calendar(data, now)
    # The endpoint has a fixed [today - one year, today] window. On a holiday
    # the last session precedes today; require a same-day source timestamp to
    # confirm coverage instead of incorrectly requiring today <= last session.
    if requested != now.date() or source.date() != requested or requested < dates[0]:
        raise DataError("Requested date is outside the verified exchange calendar.")
    previous = [day for day in dates if day < requested]
    if not previous:
        raise DataError("Previous trading session is not available in the calendar.")
    return requested in dates, previous[-1]


def _rows(data, allowed, label):
    items = data.get("item")
    if not isinstance(items, list):
        raise DataError(f"{label}: invalid item list")
    result = {}
    for row in items:
        symbol = row.get("thscode") if isinstance(row, dict) else None
        if symbol not in allowed or symbol in result:
            raise DataError(f"{label}: duplicate or unexpected symbol")
        result[symbol] = row
    return result


class RealtimeCollector:
    """One collector per watch loop; prior pools/daily bars cached only in memory.

    history_limit bounds individual previous-day daily-bar calls per session.
    Current-day data is never reused as a fallback for failed refreshes.
    """
    def __init__(self, client, max_symbols=20, history_limit=20, sleep=time.sleep):
        if type(max_symbols) is not int or not 1 <= max_symbols <= 100:
            raise ValueError("max_symbols must be 1..100")
        if type(history_limit) is not int or not 0 <= history_limit <= max_symbols:
            raise ValueError("history_limit must be 0..max_symbols")
        self.client, self.max_symbols, self.history_limit = client, max_symbols, history_limit
        self._sleep, self._context = sleep, None

    def _previous_context(self, today, prior):
        if self._context and self._context["date"] == today.isoformat():
            return self._context
        up = _pool(self.client, "limit-up-pool", prior)
        broken = _pool(self.client, "limit-break-pool", prior)
        issues = up["issues"] + broken["issues"]
        merged = {}
        for row in up["rows"]:
            merged[row["thscode"]] = {"up": row, "broken": {}}
        for row in broken["rows"]:
            merged.setdefault(row["thscode"], {"up": {}, "broken": {}})["broken"] = row
        selected = sorted(merged, key=lambda symbol: (
            -(_number(merged[symbol]["up"].get("continue_day_cnt"), integer=True) or 0),
            -(_number(merged[symbol]["up"].get("seal_money")) or 0), symbol))[:self.max_symbols]
        histories = {}
        start_ms = int(datetime.combine(prior, dt_time(), SHANGHAI).timestamp() * 1000)
        end_ms = int(datetime.combine(prior, dt_time(23, 59, 59), SHANGHAI).timestamp() * 1000)
        for symbol in selected[:self.history_limit]:
            try:
                data = self.client.get("/api/a-share/prices/historical", {
                    "thscode": symbol, "interval": "1d", "start": start_ms, "end": end_ms, "adjust": "none"})
                data_stamp = _timestamp(data.get("timestamp"))
                if data_stamp is None or data_stamp.date() != prior or data_stamp > _now():
                    raise DataError("Previous daily source timestamp is missing or invalid.")
                rows = data.get("item")
                if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
                    raise DataError("Previous daily bar missing or duplicated.")
                stamp = _timestamp(rows[0].get("date_ms"))
                if stamp is None or stamp.date() != prior:
                    raise DataError("Previous daily bar date mismatch.")
                histories[symbol] = rows[0]
            except DataError:
                issues.append(f"previous_daily:{symbol}:missing_or_invalid")
            self._sleep(.15)
        captured = _now()
        context = {"date": today.isoformat(), "previous_date": prior.isoformat(), "merged": merged,
                   "selected": selected, "histories": histories, "available_at": captured.isoformat(),
                   "complete": up["complete"] and broken["complete"], "issues": issues,
                   "universe_size": len(merged), "history_limit": self.history_limit}
        # An incomplete universe is retried at the next refresh, not permanently cached.
        if context["complete"]:
            self._context = context
        return context

    def collect(self, trading_date=None):
        started = _now()
        today = _date(trading_date) if trading_date else started.date()
        if today != started.date():
            raise DataError("Realtime collection only supports today's Shanghai date; use imported PIT records for history.")
        issues, is_trading_day, prior = [], None, None
        try:
            is_trading_day, prior = _calendar_context(self.client, today)
        except DataError as exc:
            if isinstance(exc, ApiError) and exc.code in (2001, 2003):
                raise
            issues.append("calendar:unable_to_verify_today_and_previous_session")
        session = session_status(started, is_trading_day)
        frame = {"schema_version": 1, "source": "hithink", "trading_date": today.isoformat(),
                 "previous_trading_date": prior.isoformat() if prior else None,
                 "asof": started.isoformat(), "available_at": started.isoformat(),
                 "market_asof": None, "is_trading_day": is_trading_day, "session_status": session,
                 "coverage": {"complete": False, "issues": issues}, "stocks": [], "issues": issues,
                 "provenance": {"adapter": "hithink-realtime-v1", "per_symbol_timestamp_available": False,
                                "calendar_assembly_clock_skew_tolerance_seconds": ASSEMBLY_CLOCK_SKEW_SECONDS}}
        final_auction_window = dt_time(9, 25) <= started.time().replace(tzinfo=None) < dt_time(9, 30)
        if is_trading_day is not True or session == "closed" or (session == "preopen" and not final_auction_window):
            issues.append("market:closed_or_unverified_no_active_recommendations")
            frame["coverage"]["complete"] = is_trading_day is False
            return frame
        context = self._previous_context(today, prior)
        selected = context["selected"]
        issues.extend(context["issues"])
        quotes, auction_rows, auction_data = {}, {}, {}
        quote_stamp, auction_stamp = None, None
        if selected:
            try:
                data = self.client.get("/api/a-share/prices/snapshot", {"thscodes": ",".join(selected)})
                quote_stamp = _timestamp(data.get("timestamp"))
                if quote_stamp is None or quote_stamp.date() != today or quote_stamp > _now():
                    raise DataError("Invalid current quote batch timestamp.")
                quotes = _rows(data, set(selected), "quotes")
            except DataError as exc:
                if isinstance(exc, ApiError) and exc.code in (2001, 2003):
                    raise
                quote_stamp = None
                issues.append("quotes:unavailable_or_invalid_timestamp")
            try:
                stage = "live" if started.hour * 60 + started.minute < 565 else "final"
                auction_data = self.client.get("/api/a-share/auction/snapshot", {"thscodes": ",".join(selected), "stage": stage})
                auction_stamp = _timestamp(auction_data.get("timestamp"))
                if (auction_stamp is None or auction_stamp.date() != today
                        or (auction_stamp - _now()).total_seconds() > ASSEMBLY_CLOCK_SKEW_SECONDS):
                    raise DataError("Invalid auction assembly timestamp.")
                auction_rows = _rows(auction_data, set(selected), "auction")
            except DataError as exc:
                if isinstance(exc, ApiError) and exc.code in (2001, 2003):
                    raise
                auction_data, auction_rows, auction_stamp = {}, {}, None
                issues.append("auction:unavailable_or_invalid_timestamp")
        # Intraday turnover exists only for this coverage subset; absence remains null.
        current_broken = _pool(self.client, "limit-break-pool", today)
        pool_fresh = (current_broken.get("market_timestamp_verified") is True
                      and current_broken["complete"] and bool(current_broken["timestamps"])
                      and all(0 <= (_now() - stamp).total_seconds() <= 180 for stamp in current_broken["timestamps"]))
        turnover_rows = {row["thscode"]: row for row in current_broken["rows"]} if pool_fresh else {}
        raw_turnover_rows = {row["thscode"]: row for row in current_broken["rows"]}
        if not pool_fresh:
            issues.append("intraday_turnover:current_break_pool_unavailable")
        for symbol in selected:
            prior_rows, quote, auction = context["merged"][symbol], quotes.get(symbol, {}), auction_rows.get(symbol, {})
            up, broken = prior_rows["up"], prior_rows["broken"]
            history = context["histories"].get(symbol, {})
            auction_fields = {key: _number(auction.get(key)) for key in (
                "auction_price", "auction_pct", "auction_volume", "auction_amount", "auction_unmatched",
                "auction_turnover_pct", "auction_volume_ratio", "auction_yesterday_ratio_pct")}
            auction_fields.update(auction_volume_fields(auction))
            if auction.get("auction_volume") is not None and not auction_fields["auction_volume_unit_verified"]:
                issues.append(f"auction_volume:{symbol}:share_unit_unverified")
            auction_fields.update({"data_status": auction_data.get("data_status"), "auction_phase": auction_data.get("auction_phase"),
                                   "asof": auction_stamp.isoformat() if auction_stamp else None})
            frame["stocks"].append({"symbol": symbol, "name": up.get("name") or broken.get("name"),
                "is_st": _bool(up.get("is_st")), "is_new": _bool(up.get("is_new")),
                "price": _number(quote.get("last_price")), "prev_close": _number(quote.get("prev_price")),
                "current_volume": _number(quote.get("volume")), "current_turnover": _number(quote.get("turnover")),
                "current_turnover_pct": _number(turnover_rows.get(symbol, {}).get("turnover_ratio_pct")),
                "raw_current_turnover_pct": _number(raw_turnover_rows.get(symbol, {}).get("turnover_ratio_pct")),
                "quote_asof": None, "batch_quote_asof": quote_stamp.isoformat() if quote_stamp else None,
                "theme": up.get("limit_up_reason") or None,
                "auction": auction_fields,
                "previous": {"date": prior.isoformat(), "board_count": _number(up.get("continue_day_cnt"), integer=True),
                    "open_times": _number(broken.get("open_times"), integer=True), "limit_up_time": up.get("limit_up_time"),
                    "final_seal_time": None, "volume": _number(history.get("volume")), "turnover": _number(history.get("turnover"))},
                "execution_verified": False})
        captured = _now()
        if captured.date() != today:
            raise DataError("Capture crossed trading dates; discard and refresh.")
        frame.update(asof=captured.isoformat(), available_at=captured.isoformat(),
                     market_asof=quote_stamp.isoformat() if quote_stamp else None,
                     previous_available_at=context["available_at"], session_status=session_status(captured, is_trading_day))
        frame["coverage"]["complete"] = context["complete"]
        frame["provenance"].update({"universe_size": context["universe_size"], "selected_symbols": len(selected),
            "historical_bars_limit": self.history_limit, "previous_context_cached_at": context["available_at"],
            "current_turnover_source": "current_break_pool_only", "auction_timestamp_semantics": "response_assembly_not_individual_quote",
            "current_turnover_market_time_verified": pool_fresh,
            "pool_timestamp_semantics": "response_availability_not_market_freshness",
            "auction_volume_units": "shares_only_when_price_times_quantity_matches_amount;raw_retained",
            "current_turnover_source_timestamps": [stamp.isoformat() for stamp in current_broken["timestamps"]],
            "previous_limit_up_time_semantics": "pool_limit_up_time_not_verified_final_reseal",
            "requests": list(getattr(self.client, "audit", []))[-150:]})
        issues.extend(["previous_final_seal_time:unavailable", "previous_open_times:only_previous_break_pool",
                       "current_turnover:only_current_break_pool", "per_symbol_timestamp:unavailable;research_only"])
        return frame


def collect_realtime(client, trading_date=None, max_symbols=20, history_limit=None):
    return RealtimeCollector(client, max_symbols, max_symbols if history_limit is None else history_limit).collect(trading_date)
