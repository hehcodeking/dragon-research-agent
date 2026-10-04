"""Daily, next-observed-session-open replay; never routes real orders.

replay(snapshots, config=None) returns metrics, equity_curve, orders,
signal_audit, final_state, assumptions, warnings and result_valid. Decisions
come from the preceding snapshot's available_at. Later daily close, high, low,
volume, theme and ranking features are never used for an opening buy decision.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time
from decimal import Decimal, ROUND_DOWN

from .execution import PaperBroker, SH, ZERO, decimal, iso_date, money, timestamp


def _validate_frame(frame):
    if not isinstance(frame, dict) or type(frame.get("schema_version")) is not int or frame.get("schema_version") != 1:
        raise ValueError("Every frame must be a snapshot v1 dictionary")
    if frame.get("source") not in ("demo", "hithink", "imported"):
        raise ValueError("Invalid frame source")
    day = iso_date(frame.get("trading_date"))
    asof = timestamp(frame.get("asof"))
    available = timestamp(frame.get("available_at"))
    if asof.date() != day:
        raise ValueError("Snapshot asof/trading_date mismatch")
    if available < asof:
        raise ValueError("Snapshot available_at precedes asof")
    if not isinstance(frame.get("stocks"), list):
        raise ValueError("Snapshot stocks must be a list")
    seen = set()
    for row in frame["stocks"]:
        if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
            raise ValueError("Each stock must identify its symbol")
        if row["symbol"] in seen:
            raise ValueError("Duplicate stock in frame")
        seen.add(row["symbol"])
        for field in ("price", "open", "high", "low", "prev_close", "limit_up", "limit_down"):
            if row.get(field) is not None:
                decimal(row[field], field, positive=True)
        if row.get("volume") is not None:
            decimal(row["volume"], "volume", nonnegative=True)
    return day, asof, available


def _open_quotes(frame, at):
    result = {}
    for row in frame["stocks"]:
        if row.get("open") is None:
            continue
        result[row["symbol"]] = {
            "symbol": row["symbol"], "price": row["open"], "prev_close": row.get("prev_close"),
            "limit_up": row.get("limit_up"), "limit_down": row.get("limit_down"),
            "is_st": row.get("is_st"), "is_new": row.get("is_new"),
            "suspended": row.get("suspended"), "quote_asof": at.isoformat(),
        }
    return result


def _close_quotes(frame):
    return {row["symbol"]: row for row in frame["stocks"]}


def _safe_quantity(status, quote, weight, config, cap):
    price = decimal(quote["price"], "open", positive=True)
    equity = decimal(status["equity"])
    cash = decimal(status["cash"])
    market_value = decimal(status["market_value"])
    # Fee/slippage headroom; final enforceable checks belong to PaperBroker.
    slip = decimal(config.get("slippage_bps", 5), "slippage_bps", nonnegative=True) / 10000
    fee = decimal(config.get("commission_rate", .0003), "commission_rate", nonnegative=True)
    fee += decimal(config.get("transfer_fee_rate", .00001), "transfer_fee_rate", nonnegative=True)
    cushion = decimal(config.get("min_commission", 5), "min_commission", nonnegative=True) + 2
    stock_cap = decimal(config.get("max_stock_weight", .2))
    budget = min(equity * min(weight, stock_cap), max(ZERO, equity * cap - market_value), cash)
    affordable = max(ZERO, budget - cushion) / (price * (1 + slip) * (1 + fee))
    return int((affordable / 100).to_integral_value(rounding=ROUND_DOWN)) * 100


def replay(snapshots: list[dict], config: dict | None = None) -> dict:
    """Replay input order strictly; no inferred days, bars, prices or fills.

    One snapshot describes one observed daily session. Capture-time historical
    snapshots cannot retroactively trade: their available_at must precede the
    next observed session open. An invalid snapshot raises ValueError. Missing
    execution fields create audited skips, never demo-data substitution.
    """
    from .strategy import analyze

    cfg = dict(config or {})
    if not isinstance(snapshots, list) or not snapshots:
        raise ValueError("replay requires a nonempty list of snapshots")
    frames = deepcopy(snapshots)
    validated = [_validate_frame(frame) for frame in frames]
    dates = [item[0] for item in validated]
    if any(right <= left for left, right in zip(dates, dates[1:])):
        raise ValueError("Snapshots must have unique, strictly increasing trading dates")
    for key, default in (("stop_loss_pct", .05), ("take_profit_pct", .15), ("trailing_stop_pct", .06)):
        if not ZERO < decimal(cfg.get(key, default), key) <= 1:
            raise ValueError(f"{key}: must be in (0, 1]")
    holding_limit = cfg.get("max_holding_days", 3)
    if isinstance(holding_limit, bool) or not isinstance(holding_limit, int) or holding_limit < 1:
        raise ValueError("max_holding_days: positive integer required")
    broker = PaperBroker(config=cfg)
    orders, curve, signal_audit, warnings = [], [], [], []
    pending, held_sessions, high_marks = [], {}, {}
    max_drawdown = ZERO
    stale_seen = False
    sources = sorted({frame["source"] for frame in frames})

    for index, frame in enumerate(frames):
        day, close_at, available_at = validated[index]
        day_string = day.isoformat()
        open_at = datetime.combine(day, time(9, 30), SH)
        if close_at < open_at:
            raise ValueError("Daily replay requires snapshot asof at/after session open")
        if index:
            quotes = _open_quotes(frame, open_at)
            open_risk = broker.mark_market(quotes, day_string, open_at)
            max_drawdown = max(max_drawdown, decimal(open_risk["drawdown"]))
            if "stale_position_marks" in open_risk["reasons"]:
                stale_seen = True
                warnings.append(f"{day_string}: missing/invalid held-symbol opening mark; previous mark retained and buys frozen")
            for ordinal, plan in enumerate(pending):
                symbol, side = plan["symbol"], plan["side"]
                order_id = f"replay:{plan['signal_date']}:{day_string}:{side}:{symbol}:{ordinal}"
                common = {"order_id": order_id, "symbol": symbol, "side": side,
                          "signal_date": plan["signal_date"], "signal_available_at": plan["available_at"],
                          "asof": open_at.isoformat(), "execution_date": day_string,
                          "signal_reason": plan["reason"], "target_weight": plan.get("target_weight")}
                reason = None
                status = broker.status()
                if timestamp(plan["available_at"]) >= open_at:
                    reason = "signal_not_available_before_next_session_open"
                elif symbol not in quotes:
                    reason = "missing_next_session_open"
                elif side == "buy" and symbol in status["positions"]:
                    reason = "already_held_no_pyramiding"
                elif side == "sell" and symbol not in status["positions"]:
                    reason = "position_already_closed"
                if reason:
                    orders.append({**common, "status": "skipped", "reason": reason, "fees": "0.00"})
                    continue
                if side == "sell":
                    qty = status["positions"][symbol]["sellable_quantity"]
                else:
                    qty = _safe_quantity(status, quotes[symbol], decimal(plan["target_weight"]), cfg, decimal(plan["exposure_cap"]))
                if qty <= 0:
                    orders.append({**common, "status": "skipped", "reason": "no_affordable_lot_or_sellable_shares", "fees": "0.00"})
                    continue
                order = broker.submit(order_id, symbol, side, qty, quotes[symbol], open_at,
                                      exposure_cap=plan.get("exposure_cap"))
                orders.append({**order, **common})
                if order["status"] == "filled":
                    if side == "buy":
                        held_sessions[symbol] = 0
                        high_marks[symbol] = decimal(order["fill_price"])
                    else:
                        held_sessions.pop(symbol, None)
                        high_marks.pop(symbol, None)
                max_drawdown = max(max_drawdown, decimal(broker.status()["risk"]["drawdown"]))

        close_risk = broker.mark_market(_close_quotes(frame), day_string, close_at)
        state = broker.status()
        max_drawdown = max(max_drawdown, decimal(close_risk["drawdown"]))
        if state["stale_marks"]:
            stale_seen = True
            warnings.append(f"{day_string}: missing/invalid held-symbol close mark; equity includes stale prices")
        curve.append({"trading_date": day_string, "asof": close_at.isoformat(),
                      "equity": state["equity"], "cash": state["cash"], "market_value": state["market_value"],
                      "drawdown": close_risk["drawdown"], "daily_loss": close_risk["daily_loss"],
                      "stale_marks": list(state["stale_marks"]), "allow_entries": close_risk["allow_entries"],
                      "positions": {symbol: pos["quantity"] for symbol, pos in state["positions"].items()}})
        report = analyze(frame, cfg, now=available_at, historical=True)
        signal_audit.append({"trading_date": day_string, "available_at": available_at.isoformat(),
                             "blocked": bool(report.get("blocked", True)), "regime": deepcopy(report.get("regime", {})),
                             "eligible_symbols": [row["symbol"] for row in report.get("candidates", []) if row.get("eligible") is True]})
        pending = []
        regime = report.get("regime", {})
        valid_signal = report.get("blocked") is False
        cap = min(decimal(cfg.get("max_exposure", .6)), decimal(regime.get("exposure_cap", 0), "regime exposure_cap", nonnegative=True))
        for symbol, pos in state["positions"].items():
            held_sessions[symbol] = held_sessions.get(symbol, 0) + 1
            if symbol in state["stale_marks"]:
                continue
            mark = decimal(pos["mark"])
            high_marks[symbol] = max(high_marks.get(symbol, mark), mark)
            cost = decimal(pos["average_cost"])
            reasons = []
            if mark <= cost * (1 - decimal(cfg.get("stop_loss_pct", .05))): reasons.append("close_stop_loss")
            if mark >= cost * (1 + decimal(cfg.get("take_profit_pct", .15))): reasons.append("close_take_profit")
            if mark <= high_marks[symbol] * (1 - decimal(cfg.get("trailing_stop_pct", .06))): reasons.append("close_trailing_stop")
            if held_sessions[symbol] >= holding_limit: reasons.append("max_observed_holding_sessions")
            if valid_signal and regime.get("allow_entries") is False: reasons.append("regime_exit")
            if state["daily_halted"] or state["drawdown_halted"]: reasons.append("portfolio_risk_exit")
            if reasons:
                pending.append({"symbol": symbol, "side": "sell", "signal_date": day_string,
                                "available_at": available_at.isoformat(), "reason": ",".join(reasons)})
        if valid_signal and regime.get("allow_entries") is True and close_risk["allow_entries"]:
            for candidate in report.get("candidates", []):
                if candidate.get("eligible") is not True or candidate.get("symbol") in state["positions"]:
                    continue
                weight = decimal(candidate.get("target_weight", 0), "target_weight", nonnegative=True)
                if weight <= 0:
                    continue
                if weight > 1:
                    raise ValueError("target_weight must be at most 1")
                pending.append({"symbol": candidate["symbol"], "side": "buy", "signal_date": day_string,
                                "available_at": available_at.isoformat(), "reason": "prior_available_snapshot_candidate",
                                "target_weight": float(weight), "exposure_cap": float(cap)})

    state = broker.status()
    initial = decimal(state["initial_capital"])
    counts = {key: sum(order["status"] == key for order in orders) for key in ("filled", "rejected", "skipped")}
    if pending:
        warnings.append("Final-session signals remain pending: no later supplied session exists")
    if len(frames) < 2:
        warnings.append("At least two sessions are required to observe a signal followed by a possible fill")
    assumptions = [
        "Research simulation only; no profit guarantee, live connection or real orders.",
        "Signals use prior snapshot available_at; each fills only at the immediately next supplied session open if strictly later.",
        "Input sessions are authoritative observed sessions; no exchange holiday calendar or missing sessions are inferred.",
        "Daily open is treated as observable at 09:30; explicit daily price limits and normal/ST/new/suspension flags must describe metadata known at that session open.",
        "A daily source captured later does not itself prove historical point-in-time availability; imported signal available_at must be independently supported.",
        "No opening buy at upper limit or opening sell at lower limit; no queue-position, intraday high/low or future daily-volume assumptions.",
        "Liquidity capacity, auction matching, partial fills and intraday stop execution are not modeled. Order size may be unrealistic in thin markets.",
        "Buys require 100-share lots; T+1 is enforced by dated lots; costs and adverse tick-rounded slippage are configurable estimates.",
        "Stops, take-profit, trailing exits and risk exits are decided at daily close and attempted at the next supplied open; losses may exceed thresholds on gaps/limit-down days.",
        "Missing held-symbol marks retain the previous price, invalidate performance confidence, and freeze new entries.",
        "Final holdings are marked, not forcibly liquidated; hypothetical final exit costs are not deducted.",
    ]
    return {"schema_version": 1, "mode": "daily_paper_replay", "sources": sources,
            "result_valid": not stale_seen, "performance_interpretation": "illustrative; not validated profitability",
            "metrics": {"initial_capital": str(money(initial)), "final_equity": state["equity"],
                        "total_return": float(decimal(state["equity"]) / initial - 1),
                        "max_drawdown": float(max_drawdown), "total_fees": state["total_fees"],
                        "sessions": len(frames), "filled_orders": counts["filled"],
                        "rejected_orders": counts["rejected"], "skipped_orders": counts["skipped"]},
            "equity_curve": curve, "orders": orders, "signal_audit": signal_audit,
            "pending_signals": pending, "final_state": state,
            "assumptions": assumptions, "warnings": list(dict.fromkeys(warnings))}
