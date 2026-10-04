"""Deterministic intraday watchlists. This module cannot create broker signals.

Missing factors contribute zero points but keep value=None and available=False.
Confidence measures weighted factor coverage, never a predicted probability.
"""
from collections import Counter
from datetime import date, datetime
import math
import re
from zoneinfo import ZoneInfo

from .config import validate_config
from .strategy import main_board

SH = ZoneInfo("Asia/Shanghai")
MIN_CONFIDENCE = 0.60
WEIGHTS = {
    "auction_gap": 15, "auction_amount": 15, "auction_turnover": 10,
    "auction_volume_ratio": 10, "volume_pace": 15, "current_turnover": 10,
    "previous_open_times": 15, "previous_limit_up_time": 5,
    "previous_final_seal_time": 5,
}


def _number(value, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _time(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("需要有效的 ISO 时间")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须含时区")
    return parsed.astimezone(SH)


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期必须为 YYYY-MM-DD")
    return date.fromisoformat(value)


def _seal_minute(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        return None
    hour, minute = map(int, value.split(":"))
    if hour > 23 or minute > 59:
        return None
    count = hour * 60 + minute
    return count if count == 565 or 570 <= count <= 690 or 780 <= count <= 900 else None


def _phase(now):
    seconds = now.hour * 3600 + now.minute * 60 + now.second
    if now.weekday() >= 5:
        return "closed"
    if 9 * 3600 + 15 * 60 <= seconds <= 9 * 3600 + 25 * 60:
        return "auction"
    if 9 * 3600 + 30 * 60 <= seconds <= 11 * 3600 + 30 * 60 or 13 * 3600 <= seconds <= 15 * 3600:
        return "continuous"
    if 11 * 3600 + 30 * 60 < seconds < 13 * 3600:
        return "lunch"
    return "preopen" if seconds < 9 * 3600 + 30 * 60 else "closed"


def _elapsed(now):
    minute = now.hour * 60 + now.minute + now.second / 60
    return max(0.0, min(minute - 570, 120)) + max(0.0, min(minute - 780, 120))


def _factor(key, label, value, fraction, reason):
    available = value is not None and fraction is not None
    points = round(WEIGHTS[key] * max(0.0, min(float(fraction), 1.0)), 4) if available else 0.0
    return {"key": key, "label": label, "value": value if available else None,
            "max_score": WEIGHTS[key], "score": points, "available": available, "reason": reason}


def _gap_fraction(value):
    if value < -3 or value >= 9:
        return 0.0
    if value < 0:
        return (value + 3) / 3
    if value <= 5:
        return 1.0
    return (9 - value) / 4


def recommend_realtime(frame, config=None, now=None):
    """Return a ranked, non-executable watchlist from a realtime_data frame.

    `config` uses the ordinary project configuration. Real recommendation age is
    capped at 180 seconds even when the general research age setting is looser.
    `now` is injectable for reproducible examples; the CLI must use the real clock
    for live hithink requests. No live/paper order objects are emitted here.
    """
    cfg = validate_config(config)
    if not isinstance(frame, dict):
        raise ValueError("实时数据必须为 JSON 对象")
    current = _time(now) if now is not None else datetime.now(SH)
    max_age = min(cfg["max_data_age_seconds"], 180)
    blockers, warnings = [], []
    if type(frame.get("schema_version")) is not int or frame["schema_version"] != 1:
        blockers.append("不支持的实时数据版本")
    if frame.get("source") not in ("demo", "hithink", "imported"):
        blockers.append("未知数据来源")
    phase = _phase(current)
    try:
        day, previous_day = _date(frame.get("trading_date")), _date(frame.get("previous_trading_date"))
        if previous_day >= day:
            blockers.append("上一交易日必须早于当前交易日")
        if day != current.date():
            blockers.append("实时快照与当前日期不一致")
    except (ValueError, TypeError):
        day = previous_day = None
        blockers.append("当前或上一交易日缺失/无效")
    capture = available = None
    try:
        capture, available = _time(frame.get("asof")), _time(frame.get("available_at"))
        if capture > available or available > current:
            blockers.append("采集时间、可用时间或当前时间存在未来信息")
        if day and capture.date() != day:
            blockers.append("采集日期与交易日不一致")
        if (current - capture).total_seconds() > max_age:
            blockers.append("实时采集已过期")
    except (ValueError, TypeError):
        blockers.append("缺少有效的采集/可用时间")
    if frame.get("previous_available_at") is not None:
        try:
            prev_available = _time(frame["previous_available_at"])
            if prev_available > current or (available and prev_available > available):
                blockers.append("昨日数据在本次决策时尚不可用")
        except (ValueError, TypeError):
            blockers.append("昨日数据可用时间无效")
    if frame.get("is_trading_day") is not True:
        blockers.append("当前非交易日" if frame.get("is_trading_day") is False else "交易日历尚未核验")
    if phase not in ("auction", "continuous"):
        blockers.append("当前处于非观察交易时段（盘前、午休或已收盘）")
    stated_phase = frame.get("session_status")
    if stated_phase not in ("preopen", "auction", "continuous", "lunch", "closed"):
        warnings.append("来源未提供有效时段标签，按上海时钟核验")
    elif stated_phase != phase:
        blockers.append("来源交易时段与当前时钟不一致")
    coverage = frame.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("complete") is not True:
        blockers.append("候选覆盖不完整")
    if isinstance(coverage, dict) and isinstance(coverage.get("issues"), list):
        warnings.extend(str(issue) for issue in coverage["issues"])
    if isinstance(frame.get("issues"), list):
        warnings.extend(str(issue) for issue in frame["issues"])
    batch = None
    if frame.get("market_asof") is not None:
        try:
            batch = _time(frame["market_asof"])
            if batch > current or (available and batch > available):
                blockers.append("批次行情包含未来时点")
            if day and batch.date() != day or (current - batch).total_seconds() > max_age:
                blockers.append("批次行情跨日或已过期")
        except (ValueError, TypeError):
            blockers.append("批次行情时间无效")
    else:
        warnings.append("没有批次行情时间；仅依赖逐股时戳验证，采集时间不替代行情时间")
    rows = frame.get("stocks")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("stocks 必须是对象数组")
    symbols = [row.get("symbol") for row in rows]
    if any(not isinstance(symbol, str) for symbol in symbols) or len(set(map(str, symbols))) != len(symbols):
        blockers.append("股票代码缺失或重复")
    candidates = []
    for row in rows:
        reasons, exclusions, avoid = [], [], []
        if not main_board(row.get("symbol")):
            avoid.append("超出首版沪深主板研究范围")
        for field, label in (("is_st", "ST 状态"), ("is_new", "未开板新股状态")):
            if row.get(field) is True:
                avoid.append(label + "触发排除")
            elif row.get(field) is not False:
                exclusions.append(label + "未知")
        if row.get("suspended") is True:
            avoid.append("标的已停牌")
        if _number(row.get("price"), .01) is None:
            exclusions.append("当前价格缺失或无效")
        individual_fresh = False
        if row.get("quote_asof") is None:
            exclusions.append("没有逐股行情时戳，批次最新时间不能证明该股新鲜度")
        else:
            try:
                quote_time = _time(row["quote_asof"])
                age = (current - quote_time).total_seconds()
                if quote_time > current or available and quote_time > available:
                    exclusions.append("个股行情包含未来信息")
                elif day and quote_time.date() != day or age > max_age:
                    exclusions.append("个股行情跨日或已过期")
                else:
                    individual_fresh = True
            except (ValueError, TypeError):
                exclusions.append("个股行情时间无效")
        previous = row.get("previous") if isinstance(row.get("previous"), dict) else {}
        previous_valid = previous_day is not None and previous.get("date") == previous_day.isoformat()
        if not previous_valid:
            exclusions.append("昨日特征日期与上一交易日不一致")
        height = _number(previous.get("board_count"), 0) if previous_valid else None
        if height is None or int(height) != height:
            exclusions.append("昨日连板计数未知或无效")
        elif height < 1 or height > cfg["max_board_count"]:
            avoid.append("昨日连板高度不在研究范围内")
        auction = row.get("auction") if isinstance(row.get("auction"), dict) else {}
        auction_ok = auction.get("data_status") == "ready"
        auction_reason = "来源竞价状态不是 ready；缺失不当作零值"
        auction_phase = auction.get("auction_phase")
        if auction_phase not in ("live", "final"):
            auction_ok, auction_reason = False, "竞价阶段未知"
        if auction_ok:
            try:
                assembly = _time(auction.get("asof"))
                if assembly > current or available and assembly > available or day and assembly.date() != day:
                    auction_ok, auction_reason = False, "竞价组装时间跨日或包含未来信息"
                elif auction_phase == "final" and (current.hour * 60 + current.minute < 565 or assembly.hour * 60 + assembly.minute < 565):
                    auction_ok, auction_reason = False, "9:25 前不能使用竞价终态"
                elif auction_phase == "live" and (phase != "auction" or (current - assembly).total_seconds() > max_age):
                    auction_ok, auction_reason = False, "竞价实时阶段已过期或不适用于当前时段"
                else:
                    auction_reason = "接口组装时间与阶段通过核验；不等于逐笔行情时效已证明"
            except (ValueError, TypeError):
                auction_ok, auction_reason = False, "竞价组装时间无效"
        factors = []
        missing_auction_reason = "该竞价字段缺失或数值无效" if auction_ok else auction_reason
        gap = _number(auction.get("auction_pct"), -100, 100) if auction_ok else None
        factors.append(_factor("auction_gap", "竞价涨幅（百分点）", gap,
                               _gap_fraction(gap) if gap is not None else None,
                               "0% 至 5% 得满分；负溢价或过高溢价降低分值（研究阈值）" if gap is not None else missing_auction_reason))
        amount = _number(auction.get("auction_amount"), 0) if auction_ok else None
        factors.append(_factor("auction_amount", "竞价成交额（元）", amount,
                               amount / 30_000_000 if amount is not None else None,
                               "按 3000 万元封顶计分；绝对流动性代理，不证明可成交" if amount is not None else missing_auction_reason))
        auction_turnover = _number(auction.get("auction_turnover_pct"), 0, 100) if auction_ok else None
        factors.append(_factor("auction_turnover", "竞价换手率（百分点）", auction_turnover,
                               min(auction_turnover / .5, 1) if auction_turnover is not None else None,
                               "竞价换手按 0.5% 封顶，仅表示竞价阶段" if auction_turnover is not None else missing_auction_reason))
        ratio = _number(auction.get("auction_volume_ratio"), 0) if auction_ok else None
        factors.append(_factor("auction_volume_ratio", "来源竞价量比", ratio,
                               ratio / 2 if ratio is not None else None,
                               "直接保留服务商量比，2 倍封顶；上游基准细节未独立验证" if ratio is not None else missing_auction_reason))
        yesterday_volume = _number(previous.get("volume"), .000001) if previous_valid else None
        current_volume = _number(row.get("current_volume"), 0)
        participation = current_volume / yesterday_volume * 100 if current_volume is not None and yesterday_volume else None
        elapsed = _elapsed(current)
        pace = None
        if phase == "auction":
            volume = _number(auction.get("auction_volume"), 0) if auction_ok else None
            value = volume / yesterday_volume * 100 if volume is not None and yesterday_volume else None
            factors.append(_factor("volume_pace", "竞价量占昨日全日量（%）", value,
                                   value / 1 if value is not None else None,
                                   "占昨日全日量 1% 封顶，不年化竞价量且不称为盘中量比" if value is not None else "缺有效竞价量或昨日全日成交量"))
        else:
            if phase == "continuous" and elapsed >= 15 and participation is not None:
                pace = (participation / 100) / (elapsed / 240)
            factors.append(_factor("volume_pace", "按交易分钟折算的量速启发值", pace,
                                   pace / 1.5 if pace is not None else None,
                                   "(当前量/昨日全日量)/(已过连续交易分钟/240)，1.5 封顶；非真实历史同期量比" if pace is not None else "需连续交易至少 15 分钟及有效昨日日量；不含午休分钟"))
        turnover = _number(row.get("current_turnover_pct"), 0, 100)
        factors.append(_factor("current_turnover", "当前累计换手率（百分点）", turnover,
                               min(turnover / 5, 1) if turnover is not None else None,
                               "5% 封顶；不以竞价换手替代盘中累计换手" if turnover is not None else "盘中累计换手率未提供，不使用竞价换手替代"))
        opened = _number(previous.get("open_times"), 0) if previous_valid else None
        if opened is not None and int(opened) != opened:
            opened = None
        factors.append(_factor("previous_open_times", "昨日开板次数", opened,
                               1 / (1 + opened) if opened is not None else None,
                               "15/(1+开板次数)；来源明确为零才按零计算" if opened is not None else "未匹配昨日开板次数；涨停池缺失不代表零开板"))
        pool_limit = _seal_minute(previous.get("limit_up_time")) if previous_valid else None
        final = _seal_minute(previous.get("final_seal_time")) if previous_valid else None
        for key, label, field, minute in (
            ("previous_limit_up_time", "昨日涨停时间（非最终封板）", "limit_up_time", pool_limit),
            ("previous_final_seal_time", "昨日最终封板时间", "final_seal_time", final),
        ):
            factors.append(_factor(key, label, previous.get(field) if minute is not None else None,
                                   (900 - minute) / 300 if minute is not None else None,
                                   "越早分值越高，5 分封顶；涨停池时间不保证是首次或最终封板时间" if minute is not None else "该时间未知或无效；涨停池时间不代替最终封板时间"))
        evidence_fields = {
            "auction_gap": "auction.auction_pct", "auction_amount": "auction.auction_amount",
            "auction_turnover": "auction.auction_turnover_pct", "auction_volume_ratio": "auction.auction_volume_ratio",
            "volume_pace": "auction.auction_volume,previous.volume" if phase == "auction" else "current_volume,previous.volume",
            "current_turnover": "current_turnover_pct", "previous_open_times": "previous.open_times",
            "previous_limit_up_time": "previous.limit_up_time", "previous_final_seal_time": "previous.final_seal_time",
        }
        frame_provenance = frame.get("provenance") if isinstance(frame.get("provenance"), dict) else {}
        for factor in factors:
            factor["provenance"] = {"frame_source": frame.get("source"), "fields": evidence_fields[factor["key"]]}
            if factor["key"] == "current_turnover":
                factor["provenance"]["coverage"] = frame_provenance.get("current_turnover_source", "not_independently_verified")
        completeness = sum(item["max_score"] for item in factors if item["available"]) / 100
        score = round(sum(item["score"] for item in factors), 2)
        missing = [item["label"] for item in factors if not item["available"]]
        if completeness < MIN_CONFIDENCE:
            exclusions.append(f"因子覆盖 {completeness:.0%} 低于 {MIN_CONFIDENCE:.0%}")
        if score < cfg["min_score"]:
            reasons.append(f"原始得分低于重点观察阈值 {cfg['min_score']:g}；缺失因子不重新归一化")
        if not auction_ok:
            reasons.append(auction_reason)
        if participation is not None:
            reasons.append(f"当前成交量占昨日全日量 {participation:.2f}%，这不是历史同期量比")
        if batch is not None:
            reasons.append("批次时戳仅证明某条上游数据的最新时间，不替代逐股时戳")
        exclusions.extend(avoid)
        focus = not blockers and not exclusions and individual_fresh and score >= cfg["min_score"]
        action = "回避" if avoid else "重点观察" if focus else "等待确认"
        candidates.append({**_json_safe(row), "score": score, "confidence": round(completeness, 4),
                           "confidence_label": f"因子覆盖 {completeness:.0%}（非胜率）",
                           "action": action, "recommendation_eligible": focus,
                           "eligible": False, "execution_ready": False, "target_weight": 0.0,
                           "reasons": reasons, "exclusions": exclusions, "missing_factors": missing,
                           "components": {item["key"]: item["score"] for item in factors},
                           "factors": factors, "individual_quote_fresh": individual_fresh,
                           "volume_participation_pct": participation, "normalized_volume_pace": pace,
                           "continuous_minutes_elapsed": round(elapsed, 3),
                           "auction_usable": auction_ok, "research_only": True})
    candidates.sort(key=lambda row: (-row["score"], -row["confidence"], str(row.get("symbol", ""))))
    warnings.extend([
        "仅供实时研究观察；所有候选均禁止直接执行，未生成买卖指令。",
        "得分是固定规则分，confidence 是因子覆盖率，两者都不是胜率或收益承诺。",
        "缺失因子保留 null 并贡献零分，其他权重不放大；未知开板次数不当作 0。",
        "竞价换手、盘中累计换手、量占比和服务商量比分别展示，不能互相替代。",
        "is_new 只表示未开板新股，不能独立排除上市初期所有特殊交易规则。",
    ])
    if frame.get("source") == "demo":
        warnings.insert(0, "合成演示数据和演示时钟；不代表当前市场或真实推荐。")
    report = {"schema_version": 1, "mode": "realtime_research", "source": frame.get("source"),
            "trading_date": frame.get("trading_date"), "previous_trading_date": frame.get("previous_trading_date"),
            "asof": frame.get("asof"), "decision_time": current.isoformat(),
            "market_asof": frame.get("market_asof"), "session_status": phase,
            "blocked": bool(blockers), "blockers": list(dict.fromkeys(blockers)),
            "warnings": list(dict.fromkeys(warnings)), "candidates": candidates,
            "regime": {"name": "等待数据" if blockers else "实时观察", "allow_entries": False, "exposure_cap": 0},
            "recommendation_config": {"min_score": cfg["min_score"], "min_confidence": MIN_CONFIDENCE,
                                      "max_age_seconds": max_age, "weights": dict(WEIGHTS)},
            "summary": {"stocks": len(candidates), "actions": dict(Counter(row["action"] for row in candidates))},
            "provenance": frame.get("provenance", {})}
    return _json_safe(report)
