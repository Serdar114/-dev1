# paper.py — fee calculation, paper snipe evaluation, PnL

from config import (
    SNIPE_BTC_DELTA_PCT,
    SNIPE_MAX_ASK,
    SNIPE_TIME_MIN,
    SNIPE_TIME_MAX,
)


def calc_fee(p: float) -> float:
    """
    Post March 30 2026 fee formula.
    calc_fee(0.90) → 0.00648
    calc_fee(0.85) → 0.00914 (approx)
    calc_fee(0.80) → 0.01152
    """
    return 0.072 * (p * (1 - p))


def evaluate_snipe(
    current_btc: float,
    open_btc: float,
    best_ask: float,
    time_remaining: float,
) -> dict:
    """
    Evaluate whether a paper snipe should trigger.

    Conditions (ALL must hold):
      - |btc_delta_pct| > SNIPE_BTC_DELTA_PCT  (0.10%)
      - best_ask < SNIPE_MAX_ASK                (0.93)
      - SNIPE_TIME_MIN < time_remaining < SNIPE_TIME_MAX  (2 < t < 15)

    Returns a dict with "triggered" key. If triggered, includes full details.
    Only the FIRST trigger per window is logged (enforced in window.py / main.py).
    """
    if open_btc == 0:
        return {"triggered": False}

    btc_delta_pct = (current_btc - open_btc) / open_btc * 100

    if (
        abs(btc_delta_pct) > SNIPE_BTC_DELTA_PCT
        and best_ask < SNIPE_MAX_ASK
        and SNIPE_TIME_MIN < time_remaining < SNIPE_TIME_MAX
    ):
        direction = "UP" if btc_delta_pct > 0 else "DOWN"
        entry = best_ask
        fee = calc_fee(entry)
        return {
            "triggered":               True,
            "direction":               direction,
            "time_remaining_at_trigger": round(time_remaining, 2),
            "entry_price":             round(entry, 6),
            "btc_delta_pct":           round(btc_delta_pct, 4),
            "fee":                     round(fee, 6),
        }

    return {"triggered": False}


def resolve_snipe(snipe: dict, resolution_outcome: str) -> dict:
    """
    Enrich a triggered snipe record with post-resolution PnL.
    snipe must have triggered=True.
    resolution_outcome: "UP" or "DOWN"
    """
    if not snipe.get("triggered"):
        return snipe

    direction  = snipe["direction"]
    entry      = snipe["entry_price"]
    fee        = snipe["fee"]

    paper_correct = direction == resolution_outcome

    if paper_correct:
        paper_pnl = round((1.0 - entry) - fee, 6)
    else:
        paper_pnl = round(-entry, 6)

    return {
        **snipe,
        "paper_correct":        paper_correct,
        "paper_pnl_per_share":  paper_pnl,
    }
