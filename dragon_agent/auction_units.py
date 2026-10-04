"""Verify share units before comparing provider auction volume to daily shares."""
from .data import _number


def auction_volume_fields(row):
    """Retain the provider value; never silently assume lots or multiply by 100.

    An auction executes at one price. The quantity is usable as shares only
    when price * quantity agrees with the amount (rounding tolerance 0.01%).
    Missing price/amount and the observed 100x discrepancy remain unverified.
    Readiness and time validation are separate gates in the collector/scorer.
    """
    volume = _number(row.get("auction_volume"))
    price = _number(row.get("auction_price"))
    amount = _number(row.get("auction_amount"))
    verified = (volume is not None and volume >= 0 and price is not None and price > 0
                and amount is not None and amount >= 0
                and abs(price * volume - amount) <= max(1.0, amount * 0.0001))
    return {
        "auction_volume": volume if verified else None,
        "raw_auction_volume": volume,
        "auction_volume_unit_verified": verified,
        "auction_volume_unit": "shares_amount_checked" if verified else "unverified",
    }
