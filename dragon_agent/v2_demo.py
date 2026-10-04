"""Entirely synthetic V2 evidence to demonstrate complete and partial scoring."""
from copy import deepcopy
from collections import Counter

from .model_v2 import VETOES, compare_stages, score_record
from .v2_rules import FACTORS


def attach_v2_demo(frame, report):
    result = deepcopy(report)
    day = frame["trading_date"]
    examples = {"previous_premium_pct": 3.5, "advancement_rate": .55,
                "theme_limit_count": 12, "auction_price": {"gap": 7,
                "prior_pattern": "昨日强势缩量板", "healthy_volume": True},
                "auction_amount_ratio": .04, "recovery_rate": .85,
                "first_seal_time": "09:34", "break_count": 0,
                "risk": {"reviewed": True, "deductions": []}}
    for index, candidate in enumerate(result["candidates"]):
        stages = {}
        for stage in FACTORS:
            stamp = day + ("T09:25:30+08:00" if stage == "auction_0925" else "T10:00:30+08:00")
            def evidence(value):
                return {"value": value, "source": "完全合成的 V2 演示证据",
                        "observed_at": stamp, "available_at": stamp}
            factors = {}
            for offset, spec in enumerate(FACTORS[stage]):
                if index == 2 and offset % 2 == 0:
                    continue
                if spec["kind"] == "enum":
                    choices = list(spec["options"])
                    selection = min(1 if index == 1 and stage == "auction_0925" else 0, len(choices) - 1)
                    value = choices[selection]
                else:
                    value = deepcopy(examples[spec["key"]])
                factors[spec["key"]] = evidence(value)
            vetoes = {key: evidence(index == 3 and key == "competition_lost" and stage == "intraday_1000") for key in VETOES}
            stages[stage] = score_record({"captured_at": stamp, "factors": factors, "vetoes": vetoes}, stage)
        candidate["model_v2"] = {"version": "2.0", **stages,
                                 "delta": compare_stages(stages["auction_0925"], stages["intraday_1000"])}
        if any(s["vetoes"] for s in stages.values()):
            candidate.update(action="回避", recommendation_eligible=False)
    result["model_v2"] = {"version": "2.0", "note": "完全合成的双阶段演示：展示评分变化、缺证据和一票否决，不是历史实绩。"}
    result["summary"]["actions"] = dict(Counter(row["action"] for row in result["candidates"]))
    return result
