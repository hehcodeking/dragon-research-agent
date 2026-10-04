"""Explainable cross-sectional leader research; all proposals are conditional.

No trader impersonation, trained alpha, LLM authority, or return promise. Missing
execution metadata blocks execution without fabricating the market state.
"""
from collections import Counter
from datetime import datetime
import math
import re
from zoneinfo import ZoneInfo

from .config import validate_config

SHANGHAI = ZoneInfo("Asia/Shanghai")


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("需要包含时区的 ISO 时间")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间缺少时区")
    return parsed.astimezone(SHANGHAI)


def number(value, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value if minimum is None or value >= minimum else None


def main_board(symbol):
    return bool(re.fullmatch(r"(?:(?:600|601|603|605)\d{3}\.SH|(?:000|001|002|003)\d{3}\.SZ)", str(symbol)))


def _regime(market, config):
    values = [number(market.get(k), 0) for k in ("limit_up_count", "limit_down_count", "broken_count", "max_height")]
    if any(x is None or int(x) != x for x in values):
        return {"name": "数据不足", "allow_entries": False, "exposure_cap": 0,
                "reason": "涨停、跌停、炸板数量或连板高度缺失", "break_rate": None}
    up, down, broken, height = values
    rate = broken / (up + broken) if up + broken else None
    if down >= 15 or (rate is not None and rate >= .45):
        name, cap, reason = "退潮", 0, "跌停扩散或炸板比例过高，暂停新增研究仓位"
    elif up < 15:
        name, cap, reason = "低温", 0, "涨停广度不足，等待更多确认"
    elif up >= 60 and down <= 5 and height >= 4 and rate is not None and rate <= .25:
        name, cap, reason = "活跃", .60, "涨停广度、连板高度与封板率共同达标"
    elif up >= 25 and down <= 8 and rate is not None and rate <= .30:
        name, cap, reason = "修复观察", .35, "封板结构改善；仅凭截面不能确认完整情绪周期"
    else:
        name, cap, reason = "分歧", .15, "信号不一致，降低研究仓位预算"
    return {"name": name, "allow_entries": cap > 0, "exposure_cap": min(cap, config["max_exposure"]),
            "reason": reason, "break_rate": rate}


def analyze(snapshot, config=None, now=None, historical=False):
    config = validate_config(config)
    if not isinstance(snapshot, dict):
        raise ValueError("快照必须为 JSON 对象")
    blockers, warnings = [], []
    if type(snapshot.get("schema_version")) is not int or snapshot.get("schema_version") != 1:
        blockers.append("不支持的快照版本")
    if snapshot.get("source") not in ("demo", "hithink", "imported"):
        blockers.append("未知数据来源")
    now = timestamp(now) if isinstance(now, str) else (now or datetime.now(SHANGHAI))
    if now.tzinfo is None:
        raise ValueError("now 必须含时区")
    decision = asof = None
    try:
        asof = timestamp(snapshot.get("asof"))
        decision = timestamp(snapshot.get("available_at"))
        if asof > decision:
            blockers.append("行情时间晚于可用时间，存在时间矛盾")
        if asof.date().isoformat() != snapshot.get("trading_date"):
            blockers.append("行情日期与交易日不符")
        if decision > now:
            blockers.append("信息在当前决策时间尚不可得")
        if not historical and (now - asof).total_seconds() > config["max_data_age_seconds"]:
            blockers.append("快照已过期，只能离线研究")
    except (ValueError, TypeError):
        blockers.append("快照缺少有效的行情时间或可用时间")
    provenance = snapshot.get("provenance") if isinstance(snapshot.get("provenance"), dict) else {}
    if isinstance(provenance.get("issues"), list):
        warnings.extend(str(issue) for issue in provenance["issues"])
    source_times = provenance.get("source_timestamps", {})
    if isinstance(source_times, dict):
        for source, stamps in source_times.items():
            if not isinstance(stamps, list) or not stamps:
                blockers.append(f"{source} 缺少数据时戳")
                continue
            for value in stamps:
                try:
                    stamp = timestamp(value)
                    if decision and stamp > decision:
                        blockers.append(f"{source} 数据尚未可用")
                    if stamp.date().isoformat() != snapshot.get("trading_date"):
                        blockers.append(f"{source} 数据跨日")
                    if not historical and (now - stamp).total_seconds() > config["max_data_age_seconds"]:
                        blockers.append(f"{source} 股票池已过期，最新报价不能替代市场数据新鲜度")
                except (ValueError, TypeError):
                    blockers.append(f"{source} 数据时戳无效")
    coverage = snapshot.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("complete") is not True:
        blockers.append("市场池覆盖不完整，不能把漏数当作低情绪")
    elif isinstance(coverage.get("issues"), list):
        warnings.extend(str(x) for x in coverage["issues"])
    market = snapshot.get("market") if isinstance(snapshot.get("market"), dict) else {}
    regime = _regime(market, config)
    if regime["name"] == "数据不足":
        blockers.append(regime["reason"])
    stocks = snapshot.get("stocks")
    if not isinstance(stocks, list) or any(not isinstance(x, dict) for x in stocks):
        raise ValueError("stocks 必须为对象数组")
    symbols = [s.get("symbol") for s in stocks]
    if any(not isinstance(x, str) for x in symbols) or len(set(str(x) for x in symbols)) != len(symbols):
        blockers.append("股票代码缺失或重复")
    themes = Counter(s.get("theme") for s in stocks if isinstance(s.get("theme"), str) and s.get("theme"))
    candidates = []
    for stock in stocks:
        row = dict(stock)
        reasons, excluded, execution_blocks = [], [], []
        components = {}
        if not main_board(stock.get("symbol")):
            excluded.append("首版仅研究沪深主板普通股票")
        for key, desc in (("is_st", "ST 状态"), ("is_new", "新股状态")):
            if stock.get(key) is not False:
                excluded.append(f"{desc}不符合要求或未知")
        if stock.get("suspended") is True:
            excluded.append("已停牌")
        if stock.get("suspended") is not False:
            execution_blocks.append("缺少可信的可交易/停牌状态")
        price = number(stock.get("price"), .01)
        if price is None:
            excluded.append("缺少有效价格")
        height = number(stock.get("board_count"), 0)
        if height is None or int(height) != height:
            excluded.append("连板计数缺失或无效")
            components["leader"] = 0.0
        else:
            components["leader"] = min(height / 4, 1) * 30
            if height < 2:
                excluded.append("未形成至少两板的高度优势")
            if height > config["max_board_count"]:
                excluded.append("超过连板高度风险上限")
            reasons.append(f"连板计数 {int(height)}；不等于可验证的连续涨停天数")
        theme = stock.get("theme")
        peers = themes.get(theme, 0) if isinstance(theme, str) and theme else 0
        components["theme"] = min(peers / 4, 1) * 20
        if not peers:
            excluded.append("缺少题材标签")
        reasons.append(f"同涨停原因标签共 {peers} 只，仅作主线代理")
        seal, peak = number(stock.get("seal_money"), 0), number(stock.get("max_seal_money"), .01)
        if seal is None or peak is None or seal > peak:
            components["seal"] = 0.0
            excluded.append("封单数据缺失或不一致")
        else:
            retention = seal / peak
            components["seal"] = retention * 20
            reasons.append(f"当前/峰值封单比 {retention:.0%}，不能证明排队可成交")
        turnover = number(stock.get("turnover"), 0)
        if turnover is None or turnover < config["min_turnover"]:
            excluded.append("成交额不足或缺失")
            components["liquidity"] = 0.0
        else:
            components["liquidity"] = min(turnover / (config["min_turnover"] * 4), 1) * 15
            reasons.append(f"成交额 {turnover / 100000000:.2f} 亿元")
        minute = None
        try:
            raw_time = stock.get("first_limit_time")
            if isinstance(raw_time, str) and re.fullmatch(r"\d{2}:\d{2}", raw_time):
                hour, mins = map(int, raw_time.split(":"))
                if 0 <= hour <= 23 and 0 <= mins <= 59:
                    minute = hour * 60 + mins
        except ValueError:
            pass
        if minute is None or not (minute == 565 or 570 <= minute <= 690 or 780 <= minute <= 900):
            components["timing"] = 0.0
            excluded.append("涨停时间缺失或无效")
        else:
            components["timing"] = max(0.0, min(1.0, (900 - minute) / 300)) * 15
            if asof and minute > asof.hour * 60 + asof.minute:
                excluded.append("涨停时间晚于行情截面，存在未来信息")
        quote_time = None
        try:
            quote_time = timestamp(stock.get("quote_asof"))
            if decision and quote_time > decision:
                excluded.append("个股行情使用了决策后信息")
            if quote_time.date().isoformat() != snapshot.get("trading_date"):
                excluded.append("个股行情跨交易日")
            if asof and (asof - quote_time).total_seconds() > config["max_data_age_seconds"]:
                execution_blocks.append("个股行情已过期")
        except (ValueError, TypeError):
            execution_blocks.append("没有逐股行情时间戳；批次最新时间不足以验证新鲜度")
        upper, lower = number(stock.get("limit_up"), .01), number(stock.get("limit_down"), .01)
        if upper is None or lower is None or upper <= lower or price is None or not lower <= price <= upper:
            execution_blocks.append("涨跌停价缺失、冲突或行情超出区间")
        prev_close = number(stock.get("prev_close"), .01)
        if prev_close is None:
            execution_blocks.append("前收盘价缺失")
        score = round(sum(components.values()), 2)
        if score < config["min_score"]:
            excluded.append(f"评分低于 {config['min_score']:g}")
        if not regime["allow_entries"]:
            excluded.append("情绪门禁暂停新增仓位")
        if blockers:
            excluded.extend(blockers)
        row.update({"score": score, "components": {k: round(v, 2) for k, v in components.items()},
                    "eligible": not excluded, "execution_ready": not excluded and not execution_blocks,
                    "exclusions": excluded, "execution_blocks": execution_blocks, "reasons": reasons,
                    "target_weight": 0.0, "theme_peer_count": peers})
        candidates.append(row)
    candidates.sort(key=lambda row: (-row["score"], str(row.get("symbol", ""))))
    remaining, picked = regime["exposure_cap"], 0
    theme_weights = Counter()
    for row in candidates:
        if not row["eligible"] or picked >= config["max_positions"]:
            continue
        theme = row.get("theme")
        weight = min(config["max_stock_weight"], remaining, max(0, config["max_theme_weight"] - theme_weights[theme]))
        if weight <= .000001:
            continue
        row["target_weight"] = round(weight, 6)
        remaining -= weight
        theme_weights[theme] += weight
        picked += 1
    if blockers:
        regime.update(allow_entries=False, exposure_cap=0)
    warnings.extend([
        "这是规则研究模型，评分不是上涨概率，也不构成收益保证。",
        "target_weight 为条件性研究预算；持仓、现金与成交门禁由模拟券商再次检查。",
        "涨停原因相同只代表标签相同，不等于经核验的概念板块关系。",
        "连板天梯的 seal_nextday、盘后龙虎榜和今天查到的历史修订值不可用于过去的盘中决策。",
    ])
    if snapshot.get("source") == "demo":
        warnings.insert(0, "合成演示数据：标的名称、价格与统计均为测试构造，不是市场行情。")
    return {"schema_version": 1, "source": snapshot.get("source"), "trading_date": snapshot.get("trading_date"),
            "asof": snapshot.get("asof"), "decision_time": snapshot.get("available_at"),
            "mode": "historical_research" if historical else "research",
            "blocked": bool(blockers), "blockers": blockers, "warnings": warnings,
            "market": market, "regime": regime, "candidates": candidates,
            "config": config, "provenance": snapshot.get("provenance", {}),
            "strategy": {"version": "leader-rules-v1", "weights": {"leader": 30, "theme": 20, "seal": 20, "liquidity": 15, "timing": 15},
                         "inspiration": "龙头高度、题材聚集、封单结构、成交额和情绪门禁的规则化研究"}}
