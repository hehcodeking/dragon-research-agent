"""Deterministic synthetic fixtures: never a substitute for market history."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP


def money(value):
    return float(Decimal(str(value)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def demo_snapshots():
    symbols = ["600001.SH", "600002.SH", "600003.SH", "000001.SZ", "000002.SZ", "000003.SZ", "688001.SH", "600004.SH"]
    names = ["演示龙头甲", "演示龙头乙", "演示跟随丙", "演示主线丁", "演示跟随戊", "演示弱势己", "演示科创庚", "演示风险辛"]
    prices = [12.0, 19.0, 8.0, 24.0, 15.0, 7.0, 40.0, 3.0]
    dates, current = [], date(2026, 9, 14)
    while len(dates) < 12:
        if current.weekday() < 5:
            dates.append(current.isoformat())
        current += timedelta(days=1)
    result = []
    leaders = [2, 3, 4, 0, 0, 2, 3, 4, 5, 0, 0, 2]
    close_moves = [.10, .10, .10, -.04, -.07, .10, .10, .10, .10, -.10, -.05, .10]
    for day, trading_date in enumerate(dates):
        asof = trading_date + "T15:00:00+08:00"
        market = {"limit_up_count": 75, "limit_down_count": 3, "broken_count": 14, "max_height": max(leaders[day], 4)}
        if day in (3, 4, 9, 10):
            market = {"limit_up_count": 22, "limit_down_count": 18, "broken_count": 28, "max_height": 3}
        if day == 5:
            market = {"limit_up_count": 38, "limit_down_count": 5, "broken_count": 9, "max_height": 3}
        stocks = []
        for i, symbol in enumerate(symbols):
            previous = prices[i]
            lower, upper = money(previous * .90), money(previous * 1.10)
            move = close_moves[day] if i in (0, 1, 3) else (close_moves[day] * .65 if i < 6 else .02)
            closing = min(upper, max(lower, money(previous * (1 + move))))
            opening = money(previous * (1.02 if move > 0 else -.035 + 1))
            if day == 9 and i == 0:
                opening = lower  # Conservative lower-limit sell rejection scenario.
            volume = 80000000 + i * 3000000
            stocks.append({
                "symbol": symbol, "name": names[i], "price": closing, "prev_close": previous,
                "open": opening, "high": max(opening, closing), "low": min(opening, closing),
                "volume": volume, "turnover": money(volume * (opening + closing) / 2),
                "change_pct": round((closing / previous - 1) * 100, 4), "turnover_pct": 9 + i,
                "board_count": leaders[day] if i in (0, 1, 3) else (2 if i in (2, 4) and move > 0 else 0),
                "seal_money": (90000000 - i * 5000000) if move > 0 else 0,
                "max_seal_money": 100000000, "theme": "合成算力" if i < 3 else "合成机器人",
                "first_limit_time": ["09:32", "09:45", "10:10", "09:40", "10:20", "14:50", "10:00", "11:00"][i],
                "is_st": i == 7, "is_new": False, "suspended": False,
                "quote_asof": asof, "limit_up": upper, "limit_down": lower,
            })
            prices[i] = closing
        result.append({"schema_version": 1, "source": "demo", "trading_date": trading_date,
                       "asof": asof, "available_at": asof,
                       "coverage": {"complete": True, "issues": ["全部为合成数据，市场统计不是候选数量之和"]},
                       "market": market, "stocks": stocks,
                       "provenance": {"generator": "deterministic-demo-v1", "adjustment": "none",
                                      "calendar": "synthetic weekday sessions", "disclaimer": "演示，不是真实历史行情或策略业绩"}})
    return deepcopy(result)


def realtime_demo_frame():
    """Features deliberately cover complete, incomplete and risky candidates."""
    stamp = "2026-09-18T10:00:30+08:00"
    base = demo_snapshots()[2]
    stocks = []
    for index, stock in enumerate(base["stocks"]):
        row = {key: stock[key] for key in ("symbol", "name", "is_st", "is_new", "theme")}
        row.update({"price": money(stock["price"] * 1.035), "prev_close": stock["price"],
                    "current_volume": 8000000 + 200000 * index, "current_turnover": 280000000 + index * 30000000,
                    "current_turnover_pct": 3.2 + index * .3, "quote_asof": stamp, "batch_quote_asof": stamp,
                    "auction": {"auction_pct": 3.0 + index * .5, "auction_price": money(stock["price"] * 1.03),
                                "auction_volume": 4000000, "auction_amount": 55000000,
                                "auction_turnover_pct": .75, "auction_volume_ratio": 2.1,
                                "auction_yesterday_ratio_pct": 88.0, "auction_unmatched": 80000,
                                "data_status": "ready", "auction_phase": "final", "asof": stamp},
                    "previous": {"date": "2026-09-17", "board_count": 3 if index < 4 else 2,
                                 "open_times": index % 5, "limit_up_time": "09:40", "final_seal_time": "10:20",
                                 "volume": 80000000, "turnover": 1600000000}, "execution_verified": False})
        if index in (2, 4):
            row["quote_asof"] = None
            row["current_turnover_pct"] = None
            row["previous"]["open_times"] = None
            row["previous"]["final_seal_time"] = None
        stocks.append(row)
    return {"schema_version": 1, "source": "demo", "trading_date": "2026-09-18",
            "previous_trading_date": "2026-09-17", "asof": stamp, "available_at": stamp,
            "market_asof": stamp, "previous_available_at": "2026-09-18T09:14:00+08:00",
            "is_trading_day": True, "session_status": "continuous",
            "coverage": {"complete": True, "issues": []}, "issues": [], "stocks": stocks,
            "provenance": {"generator": "synthetic-realtime-features-v1", "disclaimer": "完全合成，包含额外模拟字段，不代表实际接口具备这些字段"}}
