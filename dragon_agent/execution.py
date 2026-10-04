"""Conservative paper execution. This module never sends network orders.

PaperBroker(ledger_path=None, config=None, state=None) keeps an optional atomic,
locked JSON ledger. mark_market(quotes, trading_date, asof) establishes marks;
submit(order_id, symbol, side, quantity, quote, asof, exposure_cap=None) returns
an auditable filled/rejected record. status() returns JSON-compatible state.
Prices and money are Decimal internally and strings in the ledger/results.
Execution quotes need price, prev_close, limit_up, limit_down, quote_asof and
explicit false is_st/is_new/suspended flags. Order timestamps must be aware.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

SH = ZoneInfo("Asia/Shanghai")
CENT = Decimal("0.01")
ZERO = Decimal("0")


def decimal(value, field="value", *, positive=False, nonnegative=False):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field}: finite number required")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{field}: finite number required") from None
    if not result.is_finite():
        raise ValueError(f"{field}: finite number required")
    if positive and result <= ZERO:
        raise ValueError(f"{field}: must be positive")
    if nonnegative and result < ZERO:
        raise ValueError(f"{field}: must be nonnegative")
    return result


def money(value):
    return decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("trading_date: YYYY-MM-DD required")
    return date.fromisoformat(value)


def timestamp(value):
    if not isinstance(value, (str, datetime)):
        raise ValueError("aware ISO timestamp required")
    if isinstance(value, str) and not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", value):
        raise ValueError("extended ISO timestamp including seconds required")
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("aware ISO timestamp required") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return result.astimezone(SH)


def mainboard(symbol):
    return isinstance(symbol, str) and bool(re.fullmatch(
        r"(?:(?:600|601|603|605)\d{3}\.SH|(?:000|001|002|003)\d{3}\.SZ)", symbol))


@runtime_checkable
class BrokerAdapter(Protocol):
    """Extension contract; a real implementation needs independent integration."""

    def readiness(self) -> dict: ...
    def submit(self, **order) -> dict: ...
    def cancel(self, order_id: str) -> dict: ...
    def status(self) -> dict: ...


class DisabledLiveBroker:
    """Reserved broker boundary. There is deliberately no live order implementation."""

    def __init__(self, adapter_name="unconfigured"):
        self.adapter_name = str(adapter_name)

    def readiness(self):
        return {"ready": False, "enabled": False, "adapter": self.adapter_name,
                "reasons": ["Live broker adapter is not implemented; all live orders are disabled."],
                "requirements": ["broker-approved SDK and account permission", "trade calendar and instrument metadata",
                                 "order/fill reconciliation and idempotency", "cancel, reject and reconnect handling",
                                 "independent pre-trade risk controls and emergency stop", "paper acceptance tests"]}

    def submit(self, **order):
        raise RuntimeError("Live execution is disabled. Implement and validate a broker adapter first.")

    def cancel(self, order_id):
        raise RuntimeError("Live execution is disabled.")

    def status(self):
        return self.readiness()


def validate_adapter(adapter):
    missing = [name for name in ("readiness", "submit", "cancel", "status")
               if not callable(getattr(adapter, name, None))]
    if missing:
        return {"ready": False, "reasons": ["Missing methods: " + ", ".join(missing)]}
    result = adapter.readiness()
    if not isinstance(result, dict) or result.get("ready") is not True:
        return {"ready": False, "reasons": (result.get("reasons") if isinstance(result, dict) else None)
                or ["Adapter has not passed readiness validation."]}
    # This validates a declared interface, never enables routing in this program.
    return {"ready": True, "routing_enabled": False,
            "reasons": ["Interface readiness is self-reported; live routing remains disabled."]}


class PaperBroker:
    def __init__(self, ledger_path=None, config=None, state=None):
        self.config = dict(config or {})
        self.path = Path(ledger_path) if ledger_path else None
        self.initial = self._cfg("initial_capital", 100000, positive=True)
        for key, default in (("max_daily_loss", .02), ("max_drawdown", .08),
                             ("max_exposure", .6), ("max_stock_weight", .2)):
            if not ZERO < self._cfg(key, default) <= 1:
                raise ValueError(f"{key}: must be in (0, 1]")
        for key, default in (("commission_rate", .0003), ("min_commission", 5),
                             ("stamp_tax_rate", .0005), ("transfer_fee_rate", .00001),
                             ("slippage_bps", 5), ("quote_max_age_seconds", 180)):
            self._cfg(key, default, nonnegative=True)
        if not isinstance(self.config.get("max_positions", 3), int) or isinstance(self.config.get("max_positions", 3), bool) or self.config.get("max_positions", 3) < 1:
            raise ValueError("max_positions: positive integer required")
        self.state = deepcopy(state) if state is not None else self._new_state()
        if self.path and self.path.exists():
            self.state = self._read()
        self._validate_state()

    def _cfg(self, key, default, **checks):
        return decimal(self.config.get(key, default), key, **checks)

    def _new_state(self):
        return {"schema_version": 1, "mode": "paper", "initial_capital": str(money(self.initial)),
                "cash": str(money(self.initial)), "positions": {}, "orders": {},
                "equity_peak": str(money(self.initial)), "day_start_equity": str(money(self.initial)),
                "trading_date": None, "last_asof": None, "stale_marks": [],
                "daily_halted": False, "drawdown_halted": False, "manual_halted": False,
                "total_fees": "0.00"}

    def _read(self):
        with self.path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def _validate_state(self):
        s = self.state
        if s.get("schema_version") != 1 or s.get("mode") != "paper":
            raise ValueError("Unsupported or non-paper ledger")
        for key in ("initial_capital", "cash", "equity_peak", "day_start_equity", "total_fees"):
            decimal(s[key], key, nonnegative=True)
        if decimal(s["initial_capital"]) != money(self.initial):
            raise ValueError("Ledger initial capital does not match config")
        if s["trading_date"]:
            iso_date(s["trading_date"])
        if s["last_asof"]:
            timestamp(s["last_asof"])
        if not isinstance(s["positions"], dict) or not isinstance(s["orders"], dict):
            raise ValueError("Invalid ledger maps")
        for symbol, pos in s["positions"].items():
            if not mainboard(symbol):
                raise ValueError("Unsupported symbol in ledger")
            for key in ("mark", "average_cost"):
                decimal(pos[key], key, positive=True)
            qty = pos["quantity"]
            if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
                raise ValueError("Invalid position quantity")
            if not isinstance(pos["lots"], list) or sum(lot["quantity"] for lot in pos["lots"]) != qty:
                raise ValueError("Invalid lot quantities")
            for lot in pos["lots"]:
                if isinstance(lot["quantity"], bool) or not isinstance(lot["quantity"], int) or lot["quantity"] <= 0:
                    raise ValueError("Invalid lot quantity")
                iso_date(lot["bought_on"])
                decimal(lot["cost"], "lot cost", positive=True)
        for key in ("daily_halted", "drawdown_halted", "manual_halted"):
            if type(s[key]) is not bool:
                raise ValueError("Invalid halt flag")

    @contextmanager
    def _transaction(self):
        lock = None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock = open(str(self.path) + ".lock", "a+")
            try:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX)
            except ImportError:
                lock.close()
                raise RuntimeError("Persistent ledger locking requires POSIX fcntl")
            if self.path.exists():
                self.state = self._read()
                self._validate_state()
        before = deepcopy(self.state)
        try:
            yield
            self._validate_state()
            if self.path:
                self._save()
        except BaseException:
            self.state = before
            raise
        finally:
            if lock:
                lock.close()

    def _save(self):
        fd, name = tempfile.mkstemp(prefix="." + self.path.name, suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _check_time(self, asof):
        now = timestamp(asof)
        if self.state["last_asof"] and now < timestamp(self.state["last_asof"]):
            raise ValueError("Timestamp moves backwards relative to ledger")
        return now

    def _quote(self, symbol, quote, asof, *, for_execution=True):
        if not mainboard(symbol):
            raise ValueError("unsupported_mainboard_symbol")
        if not isinstance(quote, dict):
            raise ValueError("missing_quote")
        if quote.get("symbol", symbol) != symbol:
            raise ValueError("quote_symbol_mismatch")
        price = decimal(quote.get("price"), "price", positive=True)
        qtime = timestamp(quote.get("quote_asof"))
        age = decimal((asof - qtime).total_seconds())
        if age < 0:
            raise ValueError("future_quote")
        if qtime.date() != asof.date() or age > self._cfg("quote_max_age_seconds", 180):
            raise ValueError("stale_quote")
        if for_execution:
            if quote.get("suspended") is not False:
                raise ValueError("suspended_or_unknown")
            if quote.get("is_st") is not False or quote.get("is_new") is not False:
                raise ValueError("st_new_or_unknown")
            previous = decimal(quote.get("prev_close"), "prev_close", positive=True)
            up = decimal(quote.get("limit_up"), "limit_up", positive=True)
            down = decimal(quote.get("limit_down"), "limit_down", positive=True)
            if down >= up or price < down or price > up or not down <= previous <= up:
                raise ValueError("invalid_explicit_price_limits")
            return price, up, down
        return price

    def _equity(self):
        return decimal(self.state["cash"]) + sum(
            (decimal(pos["mark"]) * pos["quantity"] for pos in self.state["positions"].values()), ZERO)

    def _risk(self, *, latch=True):
        s = self.state
        equity = self._equity()
        peak = max(decimal(s["equity_peak"]), equity)
        start = decimal(s["day_start_equity"])
        drawdown = max(ZERO, 1 - equity / peak) if peak > 0 else ZERO
        loss = max(ZERO, 1 - equity / start) if start > 0 else ZERO
        if latch:
            s["equity_peak"] = str(money(peak))
            s["daily_halted"] |= loss >= self._cfg("max_daily_loss", .02)
            s["drawdown_halted"] |= drawdown >= self._cfg("max_drawdown", .08)
        reasons = []
        if s["manual_halted"] or self.config.get("kill_switch", False): reasons.append("manual_kill_switch")
        if s["daily_halted"]: reasons.append("daily_loss_limit")
        if s["drawdown_halted"]: reasons.append("max_drawdown_limit")
        if s["stale_marks"]: reasons.append("stale_position_marks")
        return {"allow_entries": not reasons, "reasons": reasons, "equity": str(money(equity)),
                "drawdown": float(drawdown), "daily_loss": float(loss)}

    def _refresh_staleness(self, now):
        stale = set(self.state["stale_marks"])
        for symbol, pos in self.state["positions"].items():
            try:
                qtime = timestamp(pos.get("mark_asof"))
                age = decimal((now - qtime).total_seconds())
                if qtime.date() != now.date() or age < 0 or age > self._cfg("quote_max_age_seconds", 180):
                    stale.add(symbol)
            except (ValueError, TypeError):
                stale.add(symbol)
        self.state["stale_marks"] = sorted(stale)

    def _roll_day(self, day):
        iso_date(day)
        old = self.state["trading_date"]
        if old and day < old:
            raise ValueError("Trading date moves backwards")
        if old != day:
            self.state["day_start_equity"] = str(money(self._equity()))
            self.state["daily_halted"] = False
            self.state["trading_date"] = day
            self.state["stale_marks"] = list(self.state["positions"])

    def mark_market(self, quotes, trading_date, asof, reset_day=True):
        """Mark all holdings; missing/bad marks retain last price and freeze buys.

        A new day starts from the previous stored marked equity, so opening gaps
        count in daily loss. reset_day exists for API compatibility; date roll
        is always controlled by actual timestamps, never by a caller's toggle.
        """
        with self._transaction():
            now = self._check_time(asof)
            if now.date() != iso_date(trading_date):
                raise ValueError("Timestamp and trading_date mismatch")
            self._roll_day(trading_date)
            quote_map = quotes if isinstance(quotes, dict) else {q["symbol"]: q for q in quotes}
            stale = []
            for symbol, pos in self.state["positions"].items():
                try:
                    price = self._quote(symbol, quote_map.get(symbol), now, for_execution=False)
                    pos["mark"] = str(price)
                    pos["mark_asof"] = timestamp(quote_map[symbol]["quote_asof"]).isoformat()
                except (ValueError, TypeError, KeyError):
                    stale.append(symbol)
            self.state["stale_marks"] = stale
            self.state["last_asof"] = now.isoformat()
            result = self._risk()
        return result

    def halt(self, halted=True):
        if type(halted) is not bool:
            raise ValueError("halted must be bool")
        with self._transaction():
            self.state["manual_halted"] = halted
        return self.status()

    def status(self):
        with self._transaction():
            risk = self._risk()
            result = deepcopy(self.state)
            result["equity"] = risk["equity"]
            result["risk"] = risk
            result["market_value"] = str(money(self._equity() - decimal(self.state["cash"])))
            for pos in result["positions"].values():
                day = result["trading_date"]
                pos["sellable_quantity"] = sum(lot["quantity"] for lot in pos["lots"] if day and lot["bought_on"] < day)
        return result

    def submit(self, order_id, symbol, side, quantity, quote, asof, exposure_cap=None):
        if not isinstance(order_id, str) or not order_id.strip() or len(order_id) > 200:
            raise ValueError("order_id: nonempty string up to 200 characters required")
        # A retry is the same logical order even if market quotes have changed.
        fingerprint = {"symbol": symbol, "side": side, "quantity": quantity}
        with self._transaction():
            previous = self.state["orders"].get(order_id)
            if previous:
                if previous["request"] != fingerprint:
                    raise ValueError("Idempotency conflict: order_id reused for a different order")
                return deepcopy(previous)
            result = {"order_id": order_id, "request": fingerprint, "symbol": symbol,
                      "side": side, "quantity": quantity, "status": "rejected", "reason": "", "fees": "0.00"}
            try:
                now = self._check_time(asof)
                result["asof"] = now.isoformat()
                self._roll_day(now.date().isoformat())
                if side not in ("buy", "sell"):
                    raise ValueError("side_must_be_buy_or_sell")
                clock = (now.hour, now.minute, now.second)
                if now.weekday() > 4 or not ((9, 30, 0) <= clock <= (11, 30, 0) or (13, 0, 0) <= clock <= (15, 0, 0)):
                    raise ValueError("outside_regular_trading_session")
                if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                    raise ValueError("quantity_must_be_positive_integer")
                price, up, down = self._quote(symbol, quote, now)
                if (side == "buy" and price >= up) or (side == "sell" and price <= down):
                    raise ValueError("limit_board_unfilled")
                slip = self._cfg("slippage_bps", 5) / 10000
                fill = (price * (1 + slip if side == "buy" else 1 - slip)).quantize(
                    CENT, rounding=ROUND_UP if side == "buy" else ROUND_DOWN)
                if not down <= fill <= up:
                    raise ValueError("slippage_crosses_price_limit")
                # This quote is a current mark, including for liquidation risk.
                if symbol in self.state["positions"]:
                    self.state["positions"][symbol]["mark"] = str(price)
                    self.state["positions"][symbol]["mark_asof"] = timestamp(quote["quote_asof"]).isoformat()
                    self.state["stale_marks"] = [s for s in self.state["stale_marks"] if s != symbol]
                self._refresh_staleness(now)
                risk = self._risk()
                notional = money(fill * quantity)
                commission = money(max(self._cfg("min_commission", 5), notional * self._cfg("commission_rate", .0003)))
                tax = money(notional * self._cfg("stamp_tax_rate", .0005)) if side == "sell" else ZERO
                transfer = money(notional * self._cfg("transfer_fee_rate", .00001))
                fees = commission + tax + transfer
                cash = decimal(self.state["cash"])
                pos = self.state["positions"].get(symbol)
                if side == "buy":
                    if quantity % 100:
                        raise ValueError("buy_requires_100_share_lot")
                    if not risk["allow_entries"]:
                        raise ValueError("risk_block:" + ",".join(risk["reasons"]))
                    if not pos and len(self.state["positions"]) >= self.config.get("max_positions", 3):
                        raise ValueError("max_positions")
                    if cash < notional + fees:
                        raise ValueError("insufficient_cash")
                    equity = self._equity()
                    post_equity = equity - fees - (fill - price) * quantity
                    if post_equity <= 0:
                        raise ValueError("nonpositive_post_trade_equity")
                    cap = self._cfg("max_exposure", .6)
                    if exposure_cap is not None:
                        requested = decimal(exposure_cap, "exposure_cap", nonnegative=True)
                        if requested > 1:
                            raise ValueError("invalid_exposure_cap")
                        cap = min(cap, requested)
                    stock_value = price * ((pos["quantity"] if pos else 0) + quantity)
                    if stock_value > post_equity * self._cfg("max_stock_weight", .2):
                        raise ValueError("max_stock_weight")
                    exposure = equity - cash + price * quantity
                    if exposure > post_equity * cap:
                        raise ValueError("max_exposure")
                    # Fees/slippage are included in risk before accepting entry.
                    if post_equity <= decimal(self.state["day_start_equity"]) * (1 - self._cfg("max_daily_loss", .02)):
                        raise ValueError("post_trade_daily_loss_limit")
                    if post_equity <= decimal(self.state["equity_peak"]) * (1 - self._cfg("max_drawdown", .08)):
                        raise ValueError("post_trade_drawdown_limit")
                    if not pos:
                        pos = {"quantity": 0, "average_cost": "0", "mark": str(price),
                               "mark_asof": timestamp(quote["quote_asof"]).isoformat(), "lots": []}
                        self.state["positions"][symbol] = pos
                    cost = notional + fees
                    total_cost = decimal(pos["average_cost"]) * pos["quantity"] + cost
                    pos["quantity"] += quantity
                    pos["average_cost"] = str(total_cost / pos["quantity"])
                    pos["lots"].append({"quantity": quantity, "bought_on": now.date().isoformat(), "cost": str(cost / quantity)})
                    self.state["cash"] = str(money(cash - cost))
                else:
                    available = sum(lot["quantity"] for lot in pos["lots"] if lot["bought_on"] < now.date().isoformat()) if pos else 0
                    if quantity > available:
                        raise ValueError("t_plus_one_or_insufficient_sellable_quantity")
                    if quantity % 100 and quantity != pos["quantity"]:
                        raise ValueError("sell_requires_100_shares_or_entire_odd_lot_position")
                    if cash + notional < fees:
                        raise ValueError("insufficient_cash_for_fees")
                    remaining = quantity
                    lots = []
                    for lot in pos["lots"]:
                        take = min(remaining, lot["quantity"]) if lot["bought_on"] < now.date().isoformat() else 0
                        remaining -= take
                        if lot["quantity"] > take:
                            lots.append({**lot, "quantity": lot["quantity"] - take})
                    pos["quantity"] -= quantity
                    pos["lots"] = lots
                    if pos["quantity"]:
                        pos["average_cost"] = str(sum(decimal(lot["cost"]) * lot["quantity"] for lot in lots) / pos["quantity"])
                    else:
                        del self.state["positions"][symbol]
                        self.state["stale_marks"] = [s for s in self.state["stale_marks"] if s != symbol]
                    self.state["cash"] = str(money(cash + notional - fees))
                self.state["total_fees"] = str(money(decimal(self.state["total_fees"]) + fees))
                result.update(status="filled", reason="paper_fill", fill_price=str(fill), notional=str(notional),
                              fees=str(money(fees)), commission=str(commission), stamp_tax=str(money(tax)),
                              transfer_fee=str(transfer))
                self._risk()
                self.state["last_asof"] = now.isoformat()
            except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
                result["reason"] = str(exc)
            self.state["orders"][order_id] = deepcopy(result)
        return result
