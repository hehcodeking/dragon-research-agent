"""Explicit, validated research assumptions. Rates are editable simulation inputs."""
import json
import math
from pathlib import Path

DEFAULTS = {
    "initial_capital": 100000.0,
    "max_drawdown": 0.08,
    "max_daily_loss": 0.02,
    "max_exposure": 0.60,
    "max_stock_weight": 0.20,
    "max_theme_weight": 0.35,
    "max_positions": 3,
    "min_score": 65.0,
    "min_turnover": 300000000.0,
    "max_board_count": 6,
    "max_data_age_seconds": 900,
    "quote_max_age_seconds": 180,
    "stop_loss_pct": 0.05,
    "take_profit_pct": 0.15,
    "trailing_stop_pct": 0.06,
    "max_holding_days": 3,
    "commission_rate": 0.0003,
    "min_commission": 5.0,
    "stamp_tax_rate": 0.0005,
    "transfer_fee_rate": 0.00001,
    "slippage_bps": 5.0,
}


def validate_config(values=None):
    if values is not None and not isinstance(values, dict):
        raise ValueError("配置必须是 JSON 对象")
    config = dict(DEFAULTS)
    if values:
        unknown = set(values) - set(DEFAULTS)
        if unknown:
            raise ValueError("未知配置项: " + ", ".join(sorted(unknown)))
        config.update(values)
    for key, value in config.items():
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError(f"配置 {key} 必须是有限数值")
        if value < 0:
            raise ValueError(f"配置 {key} 不能为负数")
    for key in ("max_drawdown", "max_daily_loss", "max_exposure", "max_stock_weight",
                "max_theme_weight", "stop_loss_pct", "take_profit_pct", "trailing_stop_pct"):
        if not 0 < config[key] <= 1:
            raise ValueError(f"配置 {key} 应在 (0,1] 内")
    for key in ("max_positions", "max_board_count", "max_data_age_seconds", "quote_max_age_seconds", "max_holding_days"):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError(f"配置 {key} 应为正整数")
    if config["initial_capital"] <= 0 or config["min_turnover"] <= 0:
        raise ValueError("初始资金及最低成交额必须大于零")
    if not 0 <= config["min_score"] <= 100:
        raise ValueError("min_score 应在 [0,100] 内")
    if config["max_stock_weight"] > config["max_exposure"]:
        raise ValueError("单股仓位不能超过总仓位")
    for key in ("commission_rate", "stamp_tax_rate", "transfer_fee_rate"):
        if config[key] > .1:
            raise ValueError(f"{key} 使用小数费率，不能超过 0.1")
    if config["slippage_bps"] > 1000:
        raise ValueError("滑点使用基点，不能超过 1000")
    return config


def load_config(path=None):
    return validate_config(json.loads(Path(path).read_text(encoding="utf-8")) if path else None)
