"""Evidence, freeze, provenance and research-only regression tests for V2.

Every observation below is synthetic.  These tests validate model mechanics,
not market accuracy, historical strategy efficacy or achievable returns.
"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from dragon_agent.model_v2 import (
    V2Tracker, VETOES, assessment_template, compare_stages, score_record,
)
from dragon_agent.v2_rules import FACTORS, STAGES, score_factor


DAY = "2026-09-30"
SYMBOL = "600000.SH"
AUCTION, INTRADAY = STAGES


def stamp(clock="09:25:30", day=DAY):
    return f"{day}T{clock}+08:00"


def evidence(value, at=None):
    at = at or stamp()
    return {"value": deepcopy(value), "source": "synthetic:test-observation",
            "observed_at": at, "available_at": at}


def strongest_value(spec):
    if spec["kind"] == "enum":
        return max(spec["options"], key=spec["options"].get)
    return {
        "previous_premium_pct": 4, "advancement_rate": .6,
        "theme_limit_count": 10,
        "auction_price": {"gap": 7, "prior_pattern": "昨日强势缩量板", "healthy_volume": True},
        "auction_amount_ratio": .04, "recovery_rate": .9,
        "first_seal_time": "09:32", "break_count": 0,
        "risk": {"reviewed": True, "deductions": []},
    }[spec["key"]]


def full_record(stage=AUCTION, day=DAY):
    at = stamp("09:25:30" if stage == AUCTION else "10:00:30", day)
    return {"captured_at": at, "trading_date": day, "symbol": SYMBOL, "source": "demo",
            "factors": {s["key"]: evidence(strongest_value(s), at) for s in FACTORS[stage]},
            "vetoes": {key: evidence(False, at) for key in VETOES}}


def assessments(day=DAY):
    return {"schema_version": 1, "trading_date": day,
            "stocks": {SYMBOL: {stage: full_record(stage, day) for stage in STAGES}}}


def frame(clock="09:25:30", day=DAY, source="demo"):
    at = stamp(clock, day)
    return {"schema_version": 1, "source": source, "trading_date": day,
            "available_at": at, "asof": at, "market_asof": at,
            "is_trading_day": True, "stocks": [{"symbol": SYMBOL,
                "auction": {"data_status": "ready", "auction_phase": "final",
                            "asof": stamp("09:25:00", day), "auction_amount": 40_000_000},
                "previous": {"date": "2026-09-29", "turnover": 1_000_000_000}}]}


def base_report():
    return {"candidates": [{"symbol": SYMBOL, "action": "重点观察",
                             "recommendation_eligible": True, "eligible": False,
                             "execution_ready": False, "target_weight": 0}],
            "summary": {"actions": {"重点观察": 1}}, "regime": {"allow_entries": False}}


def attach(tracker, sample, supplied=None, now=None):
    return tracker.attach(sample, base_report(), assessments=supplied,
                          now=now or datetime.fromisoformat(sample["available_at"]))


def stage_result(result, stage):
    return result["candidates"][0]["model_v2"][stage]


def factor_map(result):
    return {item["key"]: item for item in result["factors"]}


class V2RulesTests(unittest.TestCase):
    def test_two_complete_catalogues_and_category_weights(self):
        expected = {
            AUCTION: {"市场情绪": 20, "题材强度": 15, "龙头地位": 25,
                      "集合竞价": 25, "筹码/昨日结构": 10, "风险修正": 5},
            INTRADAY: {"市场实时情绪": 15, "题材实时强度": 15, "龙头地位与主动性": 20,
                       "分时承接": 20, "成交/换手结构": 10, "涨停/冲板质量": 10,
                       "板块联动": 8, "风险修正": 2},
        }
        self.assertEqual([len(FACTORS[s]) for s in STAGES], [23, 30])
        for stage in STAGES:
            actual = {}
            for spec in FACTORS[stage]:
                actual[spec["category"]] = actual.get(spec["category"], 0) + spec["max_score"]
            self.assertEqual(actual, expected[stage])
            self.assertEqual(sum(actual.values()), 100)
            result = score_record(full_record(stage), stage)
            self.assertTrue(result["complete"])
            self.assertEqual(result["coverage"], 1)
            self.assertEqual(result["score"], 100 if stage == AUCTION else 99)

    def test_enum_mappings_and_missing_values_are_explicit(self):
        for stage, specs in FACTORS.items():
            for spec in specs:
                self.assertIsNone(score_factor(stage, spec["key"], None))
                if spec["kind"] == "enum":
                    for value, expected in spec["options"].items():
                        self.assertEqual(score_factor(stage, spec["key"], value), expected)
                    self.assertIsNone(score_factor(stage, spec["key"], "未复核"))

    def test_boundaries_do_not_overstate_documented_scores(self):
        cases = [(AUCTION, "previous_premium_pct", 3, 4),
                 (AUCTION, "advancement_rate", .5, 4),
                 (AUCTION, "auction_amount_ratio", .03, 5),
                 (INTRADAY, "recovery_rate", .8, 6),
                 (INTRADAY, "first_seal_time", "09:35", 1),
                 (INTRADAY, "first_seal_time", "09:50", 0),
                 (INTRADAY, "break_count", 1, None),
                 (INTRADAY, "break_count", {"count": 1, "benign": True}, 2)]
        for stage, key, value, expected in cases:
            with self.subTest(stage=stage, key=key, value=value):
                self.assertEqual(score_factor(stage, key, value), expected)
        for value in (True, "0.5", float("nan"), float("inf"), -.1, 1.1):
            self.assertIsNone(score_factor(AUCTION, "advancement_rate", value))


class V2EvidenceTests(unittest.TestCase):
    def test_board_quality_requires_same_day_first_seal_evidence(self):
        for at in (None, stamp("09:34:00", "2026-09-29")):
            record = full_record(INTRADAY)
            if at is None:
                del record["factors"]["first_seal_time"]
            else:
                record["factors"]["first_seal_time"]["observed_at"] = at
            result = score_record(record, INTRADAY)
            quality = [f for f in result["factors"] if f["category"] == "涨停/冲板质量"]
            self.assertTrue(all(not f["available"] for f in quality))
            self.assertEqual(sum(f["score"] for f in quality), 0)

    def test_risk_has_no_default_full_score_without_explicit_review(self):
        for stage, maximum in ((AUCTION, 5), (INTRADAY, 2)):
            for value in (None, {}, {"reviewed": False, "deductions": []},
                          {"reviewed": True}, {"reviewed": 1, "deductions": []},
                          {"reviewed": True, "deductions": [{"reason": "复核", "points": maximum + 1}]},
                          {"reviewed": True, "deductions": [{"reason": "", "points": 1}]}):
                record = full_record(stage)
                record["factors"]["risk"]["value"] = value
                part = factor_map(score_record(record, stage))["risk"]
                self.assertFalse(part["available"])
                self.assertEqual(part["score"], 0)
            record = full_record(stage)
            record["factors"]["risk"]["value"] = {
                "reviewed": True, "deductions": [{"reason": "已核实风险", "points": maximum}]}
            part = factor_map(score_record(record, stage))["risk"]
            self.assertTrue(part["available"])
            self.assertEqual(part["score"], 0)

    def test_future_missing_source_and_inconsistent_timestamps_are_rejected(self):
        invalid = [
            {"available_at": stamp("09:25:31")},
            {"observed_at": stamp("09:25:31")},
            {"source": ""}, {"source": None},
            {"observed_at": "2026-09-30T09:25:00"},
            {"observed_at": stamp("09:24:59"), "available_at": stamp("09:24:58")},
        ]
        for update in invalid:
            with self.subTest(update=update):
                record = full_record()
                record["factors"]["market_position"].update(update)
                result = score_record(record, AUCTION)
                self.assertFalse(factor_map(result)["market_position"]["available"])
                self.assertEqual(result["score"], 95)
                self.assertEqual(result["coverage"], .95)
                self.assertFalse(result["complete"])

    def test_prior_day_risk_review_does_not_clear_today(self):
        record = full_record()
        record["factors"]["risk"] = evidence({"reviewed": True, "deductions": []}, stamp(day="2026-09-29"))
        record["vetoes"] = {key: evidence(False, stamp(day="2026-09-29")) for key in VETOES}
        result = score_record(record, AUCTION)
        self.assertFalse(factor_map(result)["risk"]["available"])
        self.assertEqual(result["risk_reviewed"], 0)
        self.assertFalse(result["complete"])

    def test_high_score_cannot_override_any_veto(self):
        for key in VETOES:
            record = full_record()
            record["vetoes"][key]["value"] = True
            result = score_record(record, AUCTION)
            self.assertEqual(result["score"], 100)
            self.assertEqual(result["status"], "vetoed")
            self.assertEqual(result["grade"], "一票否决")
            delta = compare_stages(result, score_record(full_record(INTRADAY), INTRADAY))
            self.assertFalse(delta["comparable"])
            self.assertIsNone(delta["value"])

    def test_unreviewed_vetoes_cannot_be_treated_as_all_clear(self):
        for invalid in (None, {"value": False}, evidence(0), evidence("false")):
            record = full_record()
            record["vetoes"]["material_bad_news"] = invalid
            result = score_record(record, AUCTION)
            self.assertEqual(result["coverage"], 1)
            self.assertFalse(result["complete"])
            self.assertEqual(result["risk_reviewed"], 9)
            self.assertEqual(result["grade"], "证据不足")

    def test_partial_scores_are_not_renormalized_or_compared(self):
        record = full_record()
        record["factors"] = {"market_position": record["factors"]["market_position"]}
        partial = score_record(record, AUCTION)
        self.assertEqual(partial["score"], 5)
        self.assertEqual(partial["coverage"], .05)
        delta = compare_stages(partial, score_record(full_record(INTRADAY), INTRADAY))
        self.assertFalse(delta["comparable"])
        self.assertIsNone(delta["value"])
        complete = compare_stages(score_record(full_record(), AUCTION),
                                  score_record(full_record(INTRADAY), INTRADAY))
        self.assertTrue(complete["comparable"])
        self.assertEqual(complete["value"], -1)

    def test_template_never_prefills_risk_or_qualitative_judgments(self):
        template = assessment_template(DAY, [SYMBOL])
        for stage in STAGES:
            item = template["stocks"][SYMBOL][stage]
            self.assertEqual(set(item["factors"]), {f["key"] for f in FACTORS[stage]})
            self.assertEqual(set(item["vetoes"]), set(VETOES))
            for observation in list(item["factors"].values()) + list(item["vetoes"].values()):
                self.assertTrue(all(value is None for value in observation.values()))


class V2TrackerTests(unittest.TestCase):
    def test_first_capture_is_frozen_and_later_evidence_is_not_backfilled(self):
        tracker = V2Tracker()
        first = attach(tracker, frame())
        original = deepcopy(stage_result(first, AUCTION))
        self.assertEqual(original["available_points"], 6)
        later = attach(tracker, frame("09:27:00"), assessments())
        self.assertEqual(stage_result(later, AUCTION), original)

    def test_capture_windows_include_start_but_exclude_end(self):
        for clock, expected in (("09:24:59", None), ("09:25:00", AUCTION),
                                ("09:29:59", AUCTION), ("09:30:00", None),
                                ("09:59:59", None), ("10:00:00", INTRADAY),
                                ("10:04:59", INTRADAY), ("10:05:00", None)):
            with self.subTest(clock=clock):
                result = attach(V2Tracker(), frame(clock))
                for stage in STAGES:
                    self.assertEqual(stage_result(result, stage) is not None, stage == expected)

    def test_missed_auction_window_cannot_be_filled_from_later_market_data(self):
        tracker = V2Tracker()
        result = attach(tracker, frame("10:00:30"), assessments())
        self.assertIsNone(stage_result(result, AUCTION))
        self.assertIsNotNone(stage_result(result, INTRADAY))
        after = attach(tracker, frame("10:06:00"), assessments())
        self.assertIsNone(stage_result(after, AUCTION))
        self.assertEqual(stage_result(after, INTRADAY), stage_result(result, INTRADAY))

    def test_not_ready_or_nonfinal_auction_does_not_freeze(self):
        for field, value in (("data_status", "not_ready"), ("auction_phase", "live")):
            tracker = V2Tracker()
            sample = frame()
            sample["stocks"][0]["auction"][field] = value
            first = attach(tracker, sample)
            self.assertIsNone(stage_result(first, AUCTION))
            second = attach(tracker, frame("09:26:00"), assessments())
            self.assertIsNotNone(stage_result(second, AUCTION))
            self.assertEqual(stage_result(second, AUCTION)["asof"], stamp("09:26:00"))

    def test_final_auction_requires_a_same_day_nonfuture_final_timestamp(self):
        for at in (None, stamp(day="2026-09-29"), stamp("09:24:59"), stamp("09:26:00")):
            with self.subTest(auction_asof=at):
                tracker = V2Tracker()
                sample = frame()
                sample["stocks"][0]["auction"]["asof"] = at
                result = attach(tracker, sample, assessments())
                self.assertIsNone(stage_result(result, AUCTION))

    def test_derived_evidence_keeps_the_actual_frame_source(self):
        for source in ("demo", "hithink"):
            result = attach(V2Tracker(), frame(source=source))
            ratio = factor_map(stage_result(result, AUCTION))["auction_amount_ratio"]
            self.assertTrue(ratio["available"])
            self.assertIn(source, ratio["source"])
            if source == "demo":
                self.assertNotIn("hithink", ratio["source"])

    def test_stale_future_or_nontrading_capture_does_not_freeze(self):
        for mode in ("stale", "future", "wrong_day", "closed", "unverified"):
            sample = frame()
            now = datetime.fromisoformat(stamp("09:28:59" if mode == "stale" else "09:26:00"))
            if mode == "future":
                sample["available_at"] = stamp("09:29:01")
            elif mode == "wrong_day":
                sample["trading_date"] = "2026-09-29"
            elif mode == "closed":
                sample["is_trading_day"] = False
            elif mode == "unverified":
                sample["is_trading_day"] = 1
            result = attach(V2Tracker(), sample, now=now)
            self.assertIsNone(stage_result(result, AUCTION))

    def test_new_day_clears_prior_scores_even_for_same_source(self):
        tracker = V2Tracker()
        prior = attach(tracker, frame(), assessments())
        self.assertIsNotNone(stage_result(prior, AUCTION))
        today = attach(tracker, frame("09:20:00", day="2026-10-09"))
        for stage in STAGES:
            self.assertIsNone(stage_result(today, stage))
        self.assertEqual(tracker.state["trading_date"], "2026-10-09")
        self.assertNotIn(AUCTION, tracker.state["stocks"].get(SYMBOL, {}))

    def test_same_source_old_day_frame_cannot_display_or_capture_today(self):
        tracker = V2Tracker()
        attach(tracker, frame(), assessments())
        result = attach(tracker, frame(), now=datetime.fromisoformat(stamp(day="2026-10-09")))
        for stage in STAGES:
            self.assertIsNone(stage_result(result, stage))

    def test_sources_are_isolated_without_blocking_first_live_capture(self):
        tracker = V2Tracker()
        attach(tracker, frame(source="demo"), assessments())
        live = attach(tracker, frame("09:26:00", source="hithink"))
        live_stage = stage_result(live, AUCTION)
        self.assertIsNotNone(live_stage)
        self.assertEqual(live_stage["asof"], stamp("09:26:00"))
        self.assertEqual(live_stage["available_points"], 6)
        self.assertFalse(live_stage["complete"])

    def test_utc_now_uses_the_same_shanghai_window(self):
        sample = frame()
        local = datetime.fromisoformat(sample["available_at"])
        a = attach(V2Tracker(), sample, assessments(), now=local)
        b = attach(V2Tracker(), sample, assessments(), now=local.astimezone(timezone.utc))
        self.assertEqual(a, b)

    def test_persisted_snapshot_survives_restart_without_being_revised(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "v2.json"
            first = attach(V2Tracker(path), frame())
            self.assertTrue(path.exists())
            second = attach(V2Tracker(path), frame("09:28:00"), assessments())
            self.assertEqual(stage_result(first, AUCTION), stage_result(second, AUCTION))

    def test_wrong_day_assessment_is_rejected(self):
        with self.assertRaises(ValueError):
            attach(V2Tracker(), frame(), assessments("2026-09-29"))

    def test_veto_decorates_watchlist_without_enabling_execution(self):
        supplied = assessments()
        supplied["stocks"][SYMBOL][AUCTION]["vetoes"]["material_bad_news"]["value"] = True
        result = attach(V2Tracker(), frame(), supplied)
        row = result["candidates"][0]
        self.assertEqual(row["action"], "回避")
        self.assertFalse(row["recommendation_eligible"])
        self.assertEqual(result["summary"]["actions"], {"回避": 1})
        self.assertFalse(row["eligible"])
        self.assertFalse(row["execution_ready"])
        self.assertEqual(row["target_weight"], 0)
        self.assertFalse(result["regime"]["allow_entries"])
        self.assertFalse(result["model_v2"]["execution_ready"])
        self.assertFalse(stage_result(result, AUCTION)["execution_ready"])
        self.assertNotIn("orders", result)

    def test_complete_dual_stage_report_is_still_research_only_and_inputs_unchanged(self):
        tracker = V2Tracker()
        supplied = assessments()
        original = deepcopy(supplied)
        attach(tracker, frame(), supplied)
        result = attach(tracker, frame("10:00:30"), supplied)
        row = result["candidates"][0]
        self.assertTrue(row["model_v2"]["delta"]["comparable"])
        self.assertEqual(row["model_v2"]["delta"]["value"], -1)
        self.assertEqual(supplied, original)
        self.assertFalse(row["execution_ready"])
        self.assertFalse(row["eligible"])
        self.assertEqual(row["target_weight"], 0)
        self.assertFalse(result["regime"]["allow_entries"])
        for stage in STAGES:
            self.assertFalse(stage_result(result, stage)["execution_ready"])
        self.assertNotIn("orders", result)


if __name__ == "__main__":
    unittest.main()
