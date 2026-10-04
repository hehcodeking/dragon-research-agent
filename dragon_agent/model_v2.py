"""Evidence-based 09:25 / 10:00 research snapshots, never order instructions."""
from copy import deepcopy
from collections import Counter
from datetime import datetime, time
import json
from pathlib import Path

from .data import SHANGHAI, SYMBOL, _number, save_json
from .v2_rules import FACTORS, score_factor

WINDOWS = {"auction_0925": (time(9, 25), time(9, 30)),
           "intraday_1000": (time(10), time(10, 5))}
VETOES = {
    "extreme_retreat": "市场极端退潮",
    "sector_core_limit_down": "板块核心批量跌停",
    "material_bad_news": "个股重大利空",
    "regulatory_risk": "重大监管风险",
    "high_open_no_support": "巨量高开后无承接",
    "record_volume_weakness": "历史天量后持续走弱",
    "broken_leader_no_sector_recovery": "龙头炸板后板块无回流",
    "repeated_limit_up_to_down": "高标连续天地板",
    "competition_lost": "同身位竞争已失败",
    "yesterday_only": "优势只来自昨日，今日未确认",
}


def _iso(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.astimezone(SHANGHAI) if stamp.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _in_window(stamp, stage):
    lower, upper = WINDOWS[stage]
    return lower <= stamp.time().replace(tzinfo=None) < upper


def _evidence(item, cutoff):
    if not isinstance(item, dict) or "value" not in item:
        return None
    observed, available = _iso(item.get("observed_at")), _iso(item.get("available_at"))
    source = item.get("source")
    if (observed is None or available is None or observed > available or available > cutoff
            or not isinstance(source, str) or not source.strip() or len(source) > 160):
        return None
    value = item["value"]
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded) > 4000:
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    return {"value": deepcopy(value), "source": source, "observed_at": observed.isoformat(),
            "available_at": available.isoformat()}


def _grade(score, stage):
    labels = (["极强龙头候选", "龙头级候选", "强核心", "有辨识度但有缺陷", "普通强势股", "跟风或风险较高"]
              if stage == "auction_0925" else
              ["市场绝对核心", "强龙头", "核心前排", "次核心", "强跟风", "跟风/弱势"])
    for lower, label in zip((90, 85, 80, 75, 70, 0), labels):
        if score >= lower:
            return label


def score_record(record, stage):
    """Score one frozen observation; external qualitative evidence stays attributed."""
    cutoff = _iso(record.get("captured_at"))
    if cutoff is None or not _in_window(cutoff, stage):
        return None
    inputs = record.get("factors", {})
    seal_evidence = _evidence(inputs.get("first_seal_time"), cutoff) if isinstance(inputs, dict) else None
    seal_verified = (seal_evidence is not None
                     and _iso(seal_evidence["observed_at"]).date() == cutoff.date()
                     and score_factor("intraday_1000", "first_seal_time", seal_evidence["value"]) is not None)
    factors, groups = [], {}
    for spec in FACTORS[stage]:
        key, category = spec["key"], spec["category"]
        evidence = _evidence(inputs.get(key), cutoff) if isinstance(inputs, dict) else None
        if key == "risk" and evidence and _iso(evidence["observed_at"]).date() != cutoff.date():
            evidence = None
        if stage == "intraday_1000" and category == "涨停/冲板质量" and not seal_verified:
            evidence = None
        points = score_factor(stage, key, evidence["value"]) if evidence else None
        available = points is not None
        factor = {"key": key, "label": spec["label"], "category": category,
                  "max_score": spec["max_score"], "score": points if available else 0,
                  "available": available, "value": evidence["value"] if evidence else None,
                  "source": evidence["source"] if evidence else None,
                  "observed_at": evidence["observed_at"] if evidence else None,
                  "available_at": evidence["available_at"] if evidence else None,
                  "reason": "按 V2.0 规则计分；保留证据来源" if available else "缺少有效、及时的证据或值不符合规则"}
        factors.append(factor)
        group = groups.setdefault(category, {"key": category, "label": category,
                                           "max_score": 0, "score": 0, "available_points": 0})
        group["max_score"] += spec["max_score"]
        group["score"] += factor["score"]
        group["available_points"] += spec["max_score"] if available else 0
    flags = record.get("vetoes", {})
    vetoes, reviewed = [], 0
    for key, label in VETOES.items():
        evidence = _evidence(flags.get(key), cutoff) if isinstance(flags, dict) else None
        if (evidence and type(evidence["value"]) is bool
                and _iso(evidence["observed_at"]).date() == cutoff.date()):
            reviewed += 1
            if evidence["value"]:
                vetoes.append(label)
    points = sum(f["max_score"] for f in factors if f["available"])
    score = round(sum(f["score"] for f in factors), 2)
    complete = points == 100 and reviewed == len(VETOES)
    status = "vetoed" if vetoes else "complete" if complete else "partial"
    return {"score": score, "coverage": points / 100, "available_points": points,
            "missing_count": sum(not f["available"] for f in factors), "complete": complete,
            "grade": _grade(score, stage) if complete and not vetoes else "一票否决" if vetoes else "证据不足",
            "status": status, "status_text": {"vetoed": "一票否决 · 剔除观察", "complete": "证据齐全 · 仅研究",
                                               "partial": "部分评分 · 待补证据"}[status],
            "asof": cutoff.isoformat(), "groups": list(groups.values()), "factors": factors,
            "vetoes": vetoes, "risk_reviewed": reviewed, "risk_review_total": len(VETOES),
            "execution_ready": False, "warnings": [
                f"一票否决核验 {reviewed}/{len(VETOES)} 项；未核验不等于无风险。",
                "人工或导入的定性判断按其声明来源展示，系统未独立验证判断真实性。",
                "覆盖不足时只展示已获分，不放大权重；评分不是胜率或买入许可。"]}


def compare_stages(auction, intraday):
    result = {"value": None, "label": "等待双阶段证据", "comparable": False,
              "reason": "两阶段均需因子齐全、一票否决完成且无否决；不同权重的分差只作研究提示。"}
    if not auction or not intraday:
        return result
    if auction["vetoes"] or intraday["vetoes"]:
        result["label"] = "一票否决优先"
        return result
    if not auction["complete"] or not intraday["complete"]:
        result["label"] = "证据不齐，暂不比较"
        return result
    a, b = auction["score"], intraday["score"]
    delta = round(b - a, 2)
    label = ("开盘兑现风险" if a >= 85 and delta <= -10 else
             "强者恒强观察" if a >= 85 and b >= 90 else
             "弱转强观察" if a < 85 and b >= 85 and delta >= 10 else
             "评分上升" if delta > 0 else "评分下降" if delta < 0 else "评分持平")
    result.update(value=delta, label=label, comparable=True)
    return result


def assessment_template(day, symbols):
    """Null-filled worksheet, never a fabricated all-clear risk assessment."""
    return {"schema_version": 1, "trading_date": day,
            "note": "每项填写 value/source/observed_at/available_at；null 是未判断。请使用真实证据，不倒填时间。",
            "stocks": {symbol: {stage: {
                "factors": {s["key"]: {"value": None, "source": None, "observed_at": None, "available_at": None}
                            for s in FACTORS[stage]},
                "vetoes": {key: {"value": None, "source": None, "observed_at": None, "available_at": None}
                           for key in VETOES}} for stage in WINDOWS} for symbol in symbols}}


class V2Tracker:
    """Freeze the first available capture in each window; persist no credentials."""
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.state = {"schema_version": 1, "trading_date": None, "stocks": {}}
        if self.path and self.path.exists():
            if self.path.stat().st_size > 2_000_000:
                raise ValueError("V2 状态文件超过大小限制")
            value = json.loads(self.path.read_text(), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
            if (not isinstance(value, dict) or value.get("schema_version") != 1
                    or not isinstance(value.get("stocks"), dict) or len(value["stocks"]) > 100):
                raise ValueError("V2 状态文件无效")
            self.state = value

    def attach(self, frame, report, assessments=None, now=None):
        current = now or datetime.now(SHANGHAI)
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise ValueError("V2 时钟必须包含时区")
        current = current.astimezone(SHANGHAI)
        captured = _iso(frame.get("available_at"))
        day = current.date().isoformat()
        if self.state["trading_date"] != day:
            self.state = {"schema_version": 1, "trading_date": day, "stocks": {}}
        assessment_stocks = {}
        if assessments is not None:
            if (not isinstance(assessments, dict) or assessments.get("schema_version") != 1
                    or assessments.get("trading_date") != day or not isinstance(assessments.get("stocks"), dict)):
                raise ValueError("V2 评估文件必须为今日、schema_version=1 且包含 stocks 对象")
            assessment_stocks = assessments["stocks"]
        stage = next((key for key in WINDOWS if _in_window(current, key)), None)
        capture_ok = (captured is not None and captured <= current and captured.date() == current.date()
                      and (current - captured).total_seconds() <= 180
                      and frame.get("trading_date") == day and frame.get("is_trading_day") is True)
        changed = False
        for stock in frame.get("stocks", []):
            symbol = stock.get("symbol")
            if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
                continue
            records = self.state["stocks"].setdefault(symbol, {})
            if not isinstance(records, dict):
                raise ValueError("V2 标的状态无效")
            if stage in records and records[stage].get("source") != frame.get("source"):
                del records[stage]
            if not stage or stage in records or not capture_ok or not _in_window(captured, stage):
                continue
            auction = stock.get("auction", {})
            if stage == "auction_0925":
                auction_at = _iso(auction.get("asof"))
                if (auction.get("data_status") != "ready" or auction.get("auction_phase") != "final"
                        or auction_at is None or auction_at.date() != current.date()
                        or auction_at > captured or auction_at.time().replace(tzinfo=None) < time(9, 25)):
                    continue
            supplied = assessment_stocks.get(symbol, {}).get(stage, {})
            supplied = supplied if isinstance(supplied, dict) else {}
            factors = deepcopy(supplied.get("factors", {}))
            if not isinstance(factors, dict):
                factors = {}
            # Directly computable objective field. Other qualitative factors
            # require an attributed observation; no inferred perfect score.
            amount, yesterday = _number(auction.get("auction_amount")), _number(stock.get("previous", {}).get("turnover"))
            if stage == "auction_0925" and amount is not None and amount >= 0 and yesterday is not None and yesterday > 0:
                factors["auction_amount_ratio"] = {
                    "value": amount / yesterday, "source": str(frame.get("source")) + ":auction_amount/previous_daily_turnover",
                    "observed_at": captured.isoformat(), "available_at": captured.isoformat()}
            record = {"captured_at": captured.isoformat(), "trading_date": day, "symbol": symbol,
                      "source": frame.get("source"), "factors": factors,
                      "vetoes": deepcopy(supplied.get("vetoes", {}))}
            # Store only known factors and timestamp-validated evidence.
            record["factors"] = {s["key"]: evidence for s in FACTORS[stage]
                                  if (evidence := _evidence(factors.get(s["key"]), captured)) is not None}
            flags = record["vetoes"] if isinstance(record["vetoes"], dict) else {}
            record["vetoes"] = {key: evidence for key in VETOES
                                 if (evidence := _evidence(flags.get(key), captured)) is not None}
            records[stage] = record
            changed = True
        if self.path and changed:
            save_json(self.path, self.state)
        result = deepcopy(report)
        for candidate in result.get("candidates", []):
            records = self.state["stocks"].get(candidate.get("symbol"), {})
            stages = {}
            for key in WINDOWS:
                record = records.get(key)
                stamp = _iso(record.get("captured_at")) if isinstance(record, dict) else None
                stages[key] = (score_record(record, key) if frame.get("trading_date") == day and stamp and stamp <= current
                               and stamp.date() == current.date() and record.get("source") == frame.get("source") else None)
            candidate["model_v2"] = {"version": "2.0", **stages,
                                     "delta": compare_stages(stages["auction_0925"], stages["intraday_1000"])}
            if any(value and value["vetoes"] for value in stages.values()):
                candidate.update(action="回避", recommendation_eligible=False)
        result["model_v2"] = {"version": "2.0", "source_document": "docs/source/leader-model-v2.md",
                              "note": "9:25 与 10:00 各自独立计分；缺证据不补分、不放大权重，一票否决优先。",
                              "capture_windows": {"auction_0925": "09:25–09:30前", "intraday_1000": "10:00–10:05前"},
                              "freeze_policy": "窗口内首次就绪记录；错过窗口不使用稍后行情回填",
                              "current_date": day, "execution_ready": False}
        result["summary"]["actions"] = dict(Counter(row["action"] for row in result.get("candidates", [])))
        return result
