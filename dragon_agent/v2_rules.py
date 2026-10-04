"""Auditable rules from the user-supplied leader model V2.0.

The two stages are independent 100-point *research* rubrics.  A returned None
means unavailable/invalid evidence, never a zero observation or a full risk
allowance.  The caller owns evidence provenance, time gates, vetoes and totals.
Overlapping boundaries and score ranges use the lower documented score.
"""
from __future__ import annotations

import math
import re
from numbers import Real
from typing import Any

STAGES = ("auction_0925", "intraday_1000")


def _enum(key: str, label: str, category: str, maximum: int,
          options: dict[str, int]) -> dict:
    return {"key": key, "label": label, "category": category,
            "max_score": maximum, "kind": "enum", "options": options}


def _number(key: str, label: str, category: str, maximum: int,
            unit: str, rule: str, kind: str = "number") -> dict:
    return {"key": key, "label": label, "category": category,
            "max_score": maximum, "kind": kind, "unit": unit, "rule": rule}


FACTORS = {
    "auction_0925": [
        _number("previous_premium_pct", "昨日涨停股平均溢价", "市场情绪", 5,
                "百分点", "<0:0；0~1:1；(1,2]:3；(2,3]:4；>3:5"),
        _number("advancement_rate", "连板晋级率", "市场情绪", 5,
                "0..1", "<0.2:0；[0.2,0.3]:1；(0.3,0.4]:3；(0.4,0.5]:4；>0.5:5"),
        _enum("high_board_feedback", "最高板/高标反馈", "市场情绪", 5, {
            "高标普遍强势高开、核心继续晋级预期强": 5,
            "高标分化，但核心有承接": 3,
            "高标明显低开、批量负反馈": 1,
            "核心高标核按钮/天地板延续": 0}),
        _enum("loss_effect", "涨跌停及亏钱效应", "市场情绪", 5, {
            "涨停明显多于跌停，亏钱效应小": 5, "情绪中性": 3,
            "跌停增加、昨日强股普遍无溢价": 1, "大面积核按钮/极端退潮": 0}),
        _enum("theme_position", "题材市场地位", "题材强度", 5, {
            "市场绝对主线": 5, "次主线": 3, "支线/轮动": 1, "无板块效应": 0}),
        _number("theme_limit_count", "题材涨停数量", "题材强度", 3,
                "只", "0~1:0；2~4:1；5~9:2；>=10:3", "integer"),
        _enum("theme_ladder", "梯队完整度", "题材强度", 3, {
            "梯队完整": 3, "有高标+中位，但断层明显": 2, "只有单一高标": 1, "无梯队": 0}),
        _enum("theme_anchor", "容量中军", "题材强度", 2, {
            "中军趋势新高/强势高开": 2, "中军中性": 1, "中军明显走弱": 0}),
        _enum("catalyst", "催化持续性", "题材强度", 2, {
            "有明确持续催化/产业逻辑": 2, "有短期消息刺激": 1, "纯情绪炒作": 0}),
        _enum("market_position", "全市场身位", "龙头地位", 5, {
            "市场最高板": 5, "次高板": 3, "板块最高板但非市场高标": 2, "非身位核心": 0}),
        _enum("sector_position", "板块地位", "龙头地位", 5, {
            "板块公认龙头": 5, "核心前排": 4, "补涨核心": 2, "跟风": 0}),
        _enum("prior_initiative", "过去1~3日主动性", "龙头地位", 5, {
            "极强主动性": 5, "较强": 4, "一般": 2, "明显被动跟随": 0}),
        _enum("recognition", "辨识度", "龙头地位", 5, {
            "市场级辨识度": 5, "板块级辨识度": 4, "有一定辨识度": 2, "普通跟风": 0}),
        _enum("competition", "竞争格局", "龙头地位", 5, {
            "已淘汰同身位竞争者": 5, "明显领先竞争者": 4, "双雄并列": 2, "竞争处于劣势": 0}),
        _number("auction_price", "竞价价格超预期", "集合竞价", 10,
                "gap为百分点", "依据昨日形态、gap与健康量能按文档保守边界计分", "auction_price"),
        _number("auction_amount_ratio", "竞价成交额/昨日成交额", "集合竞价", 6,
                "0..1", "<0.005或>0.06:0；[0.005,0.01]:1；(0.01,0.02]:3；(0.02,0.03]:5；(0.03,0.06]:6"),
        _enum("auction_trend", "9:20~9:25竞价趋势", "集合竞价", 5, {
            "价格持续上移+匹配成交增加": 5, "整体稳定偏强": 4, "价格震荡": 2, "价格不断下移": 0}),
        _enum("auction_linkage", "竞价板块联动", "集合竞价", 4, {
            "龙头+中军+跟风同步强": 4, "龙头强，中军配合": 3, "只有龙头独强": 1, "板块明显负反馈": 0}),
        _enum("prior_turnover_quality", "昨日换手质量", "筹码/昨日结构", 4, {
            "爆量分歧后强势回封": 4, "正常换手板": 3, "缩量加速": 2, "爆量未封稳": 0}),
        _enum("overhead_pressure", "套牢盘压力", "筹码/昨日结构", 2, {
            "上方压力小": 2, "有一定前高压力": 1, "巨量套牢区": 0}),
        _enum("chip_stability", "筹码稳定性", "筹码/昨日结构", 2, {
            "筹码结构健康": 2, "一般": 1, "明显松动": 0}),
        _enum("prior_close_structure", "昨日尾盘结构", "筹码/昨日结构", 2, {
            "尾盘封单稳定": 2, "有漏单但仍稳": 1, "尾盘明显抢跑": 0}),
        _number("risk", "风险修正", "风险修正", 5,
                "每项扣分0..5", "必须reviewed=true；max(0,5-扣分之和)", "risk"),
    ],
    "intraday_1000": [
        _enum("high_board_feedback", "高标反馈", "市场实时情绪", 5, {
            "高标普遍晋级/强承接": 5, "核心强、杂毛分化": 4, "高标明显走弱": 1, "批量核按钮": 0}),
        _enum("limit_structure", "涨停/跌停结构", "市场实时情绪", 4, {
            "涨停快速扩散，跌停少": 4, "中性": 2, "跌停扩散": 0}),
        _enum("prior_strong_feedback", "昨日强股反馈", "市场实时情绪", 3, {
            "昨日强股多数给溢价": 3, "分化": 2, "普遍负反馈": 0}),
        _enum("break_rate", "炸板率", "市场实时情绪", 3, {
            "炸板率低": 3, "中性": 2, "炸板率明显升高": 0}),
        _enum("theme_new_limits", "10点前新增涨停", "题材实时强度", 4, {
            "板块持续新增涨停": 4, "有1~2只助攻": 2, "无新增": 0}),
        _enum("theme_ladder", "梯队晋级", "题材实时强度", 4, {
            "高中低梯队同步强化": 4, "龙头强、低位有助攻": 3, "只有龙头": 1, "梯队坍塌": 0}),
        _enum("theme_anchor", "容量中军", "题材实时强度", 3, {
            "中军放量上涨/突破": 3, "中性": 1, "中军跳水": 0}),
        _enum("theme_index", "板块指数/整体涨幅", "题材实时强度", 2, {
            "强": 2, "中性": 1, "弱": 0}),
        _enum("theme_diffusion", "题材扩散性（人工等级）", "题材实时强度", 2, {
            "人工评为强": 2, "人工评为中": 1, "人工评为弱": 0}),
        _enum("initiative", "主动领涨", "龙头地位与主动性", 5, {
            "率先上板/率先拉升": 5, "与核心同步": 3, "被动跟随": 0}),
        _enum("resilience", "分歧时抗跌", "龙头地位与主动性", 5, {
            "板块跳水时明显抗跌": 5, "正常同步": 3, "跌幅大于板块": 0}),
        _enum("influence", "带动性", "龙头地位与主动性", 5, {
            "拉升明显带动板块": 5, "有一定带动": 3, "完全无带动": 0}),
        _enum("competition", "卡位结果", "龙头地位与主动性", 5, {
            "明显卡掉竞争者": 5, "暂时领先": 3, "双雄未分胜负": 2, "被竞争者卡掉": 0}),
        _enum("pullback_support", "回撤承接", "分时承接", 8, {
            "强承接": 7, "较强": 5, "一般": 3, "弱": 0}),
        _number("recovery_rate", "回撤修复率", "分时承接", 6,
                "0..1", "<0.2:0；[0.2,0.4]:1；(0.4,0.6]:3；(0.6,0.8):5；>=0.8:6"),
        _enum("vwap_position", "均价线表现", "分时承接", 3, {
            "大部分时间运行于均价线上方": 3, "多次争夺均价线": 2, "长期压在均价线下": 0}),
        _enum("selloff_volume", "下杀量价", "分时承接", 3, {
            "下杀缩量、拉升放量": 3, "量价中性": 1, "下杀放量、反弹缩量": 0}),
        _enum("opening_amount", "开盘成交额占比与价格", "成交/换手结构", 3, {
            "放量且价格强": 3, "适度放量": 2, "巨量滞涨": 0}),
        _enum("turnover_quality", "换手充分度", "成交/换手结构", 3, {
            "分歧充分且承接住": 3, "换手一般": 2, "极端缩量/极端爆量失控": 0}),
        _enum("price_volume", "价格与成交同步", "成交/换手结构", 4, {
            "放量上涨、缩量回调": 4, "量价正常": 2, "放量下跌、反弹无量": 0}),
        _number("first_seal_time", "首次封板时间", "涨停/冲板质量", 2,
                "HH:MM", "09:25~09:34:2；09:35~09:49:1；09:50~10:00:0", "time"),
        _enum("seal_initiative", "封板主动性", "涨停/冲板质量", 2, {
            "主动扫板": 2, "被动推板": 1}),
        _number("break_count", "炸板次数", "涨停/冲板质量", 2,
                "次", "0:2；1需独立确认良性才2，否则缺失；2:1；>=3:0", "integer"),
        _enum("reseal_speed", "回封速度", "涨停/冲板质量", 2, {
            "快速回封": 2, "震荡后回封": 1, "无法回封": 0}),
        _enum("seal_stability", "封板稳定性", "涨停/冲板质量", 2, {
            "封单稳定、漏单少": 2, "一般": 1, "持续漏单": 0}),
        _enum("follower_linkage", "龙头拉升带动跟风", "板块联动", 3, {
            "明显带动": 3, "一般": 1, "无": 0}),
        _enum("anchor_linkage", "带动中军", "板块联动", 2, {
            "明显": 2, "一般": 1, "无": 0}),
        _enum("low_board_linkage", "带动首板/补涨", "板块联动", 2, {
            "有明显助攻": 2, "一般": 1, "无": 0}),
        _enum("index_linkage", "板块指数同步", "板块联动", 1, {
            "同步回流": 1, "不跟": 0}),
        _number("risk", "风险修正", "风险修正", 2,
                "每项扣分0..2", "必须reviewed=true；max(0,2-扣分之和)", "risk"),
    ],
}

_INDEX = {stage: {item["key"]: item for item in factors}
          for stage, factors in FACTORS.items()}


def _finite(value: Any, low: float | None = None,
            high: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if low is not None and number < low or high is not None and number > high:
        return None
    return number


def _auction_price(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    gap = _finite(value.get("gap"), -100, 100)
    pattern = value.get("prior_pattern")
    if gap is None:
        return None
    if pattern == "昨日强势缩量板":
        if gap < 0:
            return 0.0
        if gap <= 2:
            return 3.0
        if gap <= 4:
            return 6.0
        if gap <= 6:
            return 8.0
        if gap <= 9 and value.get("healthy_volume") is True:
            return 10.0
        # The source does not define >9% or the unhealthy-volume 6~9% case.
        return None
    if pattern == "昨日爆量烂板/大分歧":
        if gap < -1:
            return 0.0
        if gap <= 1:
            return 3.0
        if gap <= 3:
            return 6.0
        if gap <= 5:
            return 8.0
        return 10.0
    return None


def _risk(value: Any, maximum: float) -> float | None:
    if not isinstance(value, dict) or value.get("reviewed") is not True:
        return None
    deductions = value.get("deductions")
    if not isinstance(deductions, list):
        return None
    total = 0.0
    for item in deductions:
        if not isinstance(item, dict):
            return None
        reason = item.get("reason")
        points = _finite(item.get("points"), 0, maximum)
        if not isinstance(reason, str) or not reason.strip() or points is None:
            return None
        total += points
    return max(0.0, maximum - total)


def score_factor(stage: str, key: str, value: Any) -> float | None:
    """Score a single reviewed observation; invalid/missing values return None.

    Enum labels are exact strings from FACTORS.  Numbers never accept strings
    or booleans.  Percent-point and ratio units are deliberately distinct.
    The optional break-count object supports {count: 1, benign: true}; a bare
    count of one lacks the source model's required "benign turnover" evidence.
    Unknown stage/key returns None to keep external evidence fail-closed.
    """
    if not isinstance(stage, str) or not isinstance(key, str):
        return None
    factor = _INDEX.get(stage, {}).get(key)
    if factor is None or value is None:
        return None
    kind = factor["kind"]
    if kind == "enum":
        if not isinstance(value, str) or value not in factor["options"]:
            return None
        return float(factor["options"][value])
    if kind == "risk":
        return _risk(value, float(factor["max_score"]))
    if kind == "auction_price":
        return _auction_price(value)
    if kind == "time":
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
            return None
        hour, minute = map(int, value.split(":"))
        stamp = hour * 60 + minute
        if hour > 23 or minute > 59 or not 565 <= stamp <= 600:
            return None
        return 2.0 if stamp < 575 else 1.0 if stamp < 590 else 0.0
    if key == "break_count":
        benign = value.get("benign") if isinstance(value, dict) else None
        count = _finite(value.get("count") if isinstance(value, dict) else value, 0)
        if count is None or not count.is_integer():
            return None
        if count == 0:
            return 2.0
        if count == 1:
            return 2.0 if benign is True else None
        return 1.0 if count == 2 else 0.0
    if key == "theme_limit_count":
        count = _finite(value, 0)
        if count is None or not count.is_integer():
            return None
        return 3.0 if count >= 10 else 2.0 if count >= 5 else 1.0 if count >= 2 else 0.0
    if key == "previous_premium_pct":
        number = _finite(value, -100, 100)
        if number is None:
            return None
        return (0.0 if number < 0 else 1.0 if number <= 1 else
                3.0 if number <= 2 else 4.0 if number <= 3 else 5.0)
    number = _finite(value, 0, 1)
    if number is None:
        return None
    if key == "advancement_rate":
        return (0.0 if number < .2 else 1.0 if number <= .3 else
                3.0 if number <= .4 else 4.0 if number <= .5 else 5.0)
    if key == "auction_amount_ratio":
        return (0.0 if number < .005 or number > .06 else
                1.0 if number <= .01 else 3.0 if number <= .02 else
                5.0 if number <= .03 else 6.0)
    if key == "recovery_rate":
        return (0.0 if number < .2 else 1.0 if number <= .4 else
                3.0 if number <= .6 else 5.0 if number < .8 else 6.0)
    return None
