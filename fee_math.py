"""
fee_math.py - Exact Polymarket taker fee mathematics.

Sources:
  Polymarket CLOB docs: https://docs.polymarket.com/
  Fee model: 2% taker fee on USDC notional of the order.
             Maker fee: 0%.
             No rebates.

Key definitions:
  price_per_share  : the limit price of the order (0 < p < 1), in USDC per share
  shares           : number of outcome shares
  notional_usdc    : price_per_share * shares  (the raw cost before fee)
  taker_fee_usdc   : notional_usdc * TAKER_FEE_RATE
  total_cost_usdc  : notional_usdc + taker_fee_usdc  (what you actually pay)
  net_shares       : shares  (you always receive the number of shares you ordered)

For a WIN (share settles at $1.00):
  gross_pnl_usdc   : shares * 1.0  - total_cost_usdc
  net_pnl_usdc     : gross_pnl_usdc  (no exit fee at settlement)

For a LOSS (share settles at $0.00):
  net_pnl_usdc     : -total_cost_usdc

Break-even price (before fee):
  You need: price_per_share * (1 + TAKER_FEE_RATE) < 1.0  to have positive expected value
  => break_even_price = 1 / (1 + TAKER_FEE_RATE) = 1/1.02 ≈ 0.9804

  More useful: for a given true probability p_true,
  the fair price is p_true.
  You need: p_true > ask_price * (1 + TAKER_FEE_RATE) to have positive EV.

Sell side (closing a position or taking NO):
  You receive:  price_per_share * shares
  Fee:          price_per_share * shares * TAKER_FEE_RATE
  Net received: price_per_share * shares * (1 - TAKER_FEE_RATE)

All functions return named dicts. No floats rounded unless explicitly named _rounded.
"""

from dataclasses import dataclass, asdict
from typing import Dict, Optional
import json

import config


# ── core data structure ───────────────────────────────────────────────────────

@dataclass
class OrderCost:
    """Full breakdown of a taker order's economics."""

    # inputs
    side: str                      # "BUY" or "SELL"
    price_per_share: float         # limit price 0 < p < 1
    shares: float                  # number of shares
    taker_fee_rate: float          # e.g. 0.02

    # computed
    notional_usdc: float = 0.0
    taker_fee_usdc: float = 0.0
    total_cost_usdc: float = 0.0   # for BUY: what you pay out of bankroll
    net_received_usdc: float = 0.0 # for SELL: what lands in bankroll after fee

    # settlement scenarios (for BUY side)
    pnl_if_win: Optional[float] = None    # share settles at $1.00
    pnl_if_loss: Optional[float] = None   # share settles at $0.00

    # break-even
    break_even_true_prob: Optional[float] = None
    edge_at_true_prob: Optional[float] = None  # requires caller to supply true_prob

    # diagnostics
    roi_if_win: Optional[float] = None    # pnl_if_win / total_cost_usdc
    effective_price: Optional[float] = None  # total_cost_usdc / shares

    def to_dict(self) -> Dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


# ── core computation ──────────────────────────────────────────────────────────

def compute_taker_buy(
    price_per_share: float,
    shares: float,
    true_prob: Optional[float] = None,
    fee_rate: float = config.TAKER_FEE_RATE,
) -> OrderCost:
    """
    Compute full cost and PnL for a TAKER BUY order.

    Args:
        price_per_share : ask price in USDC per share (0 < p < 1)
        shares          : number of YES (or NO) shares to buy
        true_prob       : your estimate of true win probability (optional)
        fee_rate        : taker fee rate (default from config)

    Returns:
        OrderCost dataclass with all fields populated.
    """
    if not (0 < price_per_share < 1):
        raise ValueError(f"price_per_share must be in (0, 1), got {price_per_share}")
    if shares <= 0:
        raise ValueError(f"shares must be > 0, got {shares}")
    if not (0 <= fee_rate < 1):
        raise ValueError(f"fee_rate must be in [0, 1), got {fee_rate}")

    notional     = price_per_share * shares
    fee          = notional * fee_rate
    total_cost   = notional + fee
    eff_price    = total_cost / shares

    # Settlement PnL (shares pay out $1.00 on win, $0.00 on loss)
    pnl_win  = (shares * 1.0) - total_cost
    pnl_loss = -total_cost

    # Break-even: what true prob makes EV = 0?
    # EV = p_true * 1 - total_cost/shares = 0  =>  p_true = eff_price
    be_prob = eff_price  # i.e. p_true must exceed this for positive EV

    edge = None
    if true_prob is not None:
        # edge = true_prob * 1.0 - eff_price (per share)
        edge = true_prob - eff_price

    roi = pnl_win / total_cost if total_cost > 0 else None

    return OrderCost(
        side="BUY",
        price_per_share=price_per_share,
        shares=shares,
        taker_fee_rate=fee_rate,
        notional_usdc=round(notional, 8),
        taker_fee_usdc=round(fee, 8),
        total_cost_usdc=round(total_cost, 8),
        net_received_usdc=0.0,
        pnl_if_win=round(pnl_win, 8),
        pnl_if_loss=round(pnl_loss, 8),
        break_even_true_prob=round(be_prob, 8),
        edge_at_true_prob=round(edge, 8) if edge is not None else None,
        roi_if_win=round(roi, 6) if roi is not None else None,
        effective_price=round(eff_price, 8),
    )


def compute_taker_sell(
    price_per_share: float,
    shares: float,
    fee_rate: float = config.TAKER_FEE_RATE,
) -> OrderCost:
    """
    Compute net USDC received for a TAKER SELL order.

    Used to value exit trades and NO-side entries (buy NO = sell YES).
    """
    if not (0 < price_per_share < 1):
        raise ValueError(f"price_per_share must be in (0, 1), got {price_per_share}")
    if shares <= 0:
        raise ValueError(f"shares must be > 0, got {shares}")

    notional   = price_per_share * shares
    fee        = notional * fee_rate
    net_recv   = notional - fee

    return OrderCost(
        side="SELL",
        price_per_share=price_per_share,
        shares=shares,
        taker_fee_rate=fee_rate,
        notional_usdc=round(notional, 8),
        taker_fee_usdc=round(fee, 8),
        total_cost_usdc=0.0,
        net_received_usdc=round(net_recv, 8),
    )


# ── bankroll-constrained position sizing ─────────────────────────────────────

def max_shares_from_usdc(
    usdc_budget: float,
    ask_price: float,
    fee_rate: float = config.TAKER_FEE_RATE,
) -> float:
    """
    Given a USDC budget and an ask price, compute the maximum shares
    you can buy as a taker, including fee.

    total_cost = shares * ask_price * (1 + fee_rate) = usdc_budget
    => shares = usdc_budget / (ask_price * (1 + fee_rate))
    """
    if ask_price <= 0 or ask_price >= 1:
        raise ValueError(f"ask_price must be in (0, 1), got {ask_price}")
    if usdc_budget <= 0:
        raise ValueError(f"usdc_budget must be > 0, got {usdc_budget}")
    return usdc_budget / (ask_price * (1 + fee_rate))


def usdc_for_shares(
    shares: float,
    ask_price: float,
    fee_rate: float = config.TAKER_FEE_RATE,
) -> float:
    """USDC you need to spend (including fee) to buy a given number of shares."""
    return shares * ask_price * (1 + fee_rate)


# ── min_order_size constraint check ──────────────────────────────────────────

def check_min_size(
    shares: float,
    min_shares: float,
    ask_price: float,
    usdc_budget: float,
    fee_rate: float = config.TAKER_FEE_RATE,
) -> Dict:
    """
    Check whether a desired share quantity clears the min_order_size constraint
    under the bankroll and fee model.

    Returns a dict with:
      viable         : bool – can we place this order?
      reason         : str  – why not, if not viable
      shares_wanted  : float
      shares_floored : float (floored to min_shares if below)
      cost_usdc      : float (total including fee)
      budget_ok      : bool
      min_size_ok    : bool
    """
    shares_floored = max(shares, min_shares)
    cost = usdc_for_shares(shares_floored, ask_price, fee_rate)
    budget_ok   = cost <= usdc_budget
    min_size_ok = shares >= min_shares

    viable = budget_ok and min_size_ok

    reasons = []
    if not min_size_ok:
        reasons.append(f"shares {shares:.4f} < min_order_size {min_shares:.4f}")
    if not budget_ok:
        reasons.append(f"cost {cost:.4f} USDC > budget {usdc_budget:.4f} USDC")

    return {
        "viable":        viable,
        "reason":        "; ".join(reasons) if reasons else "ok",
        "shares_wanted": round(shares, 6),
        "shares_floored": round(shares_floored, 6),
        "cost_usdc":     round(cost, 6),
        "budget_ok":     budget_ok,
        "min_size_ok":   min_size_ok,
    }


# ── round-trip PnL (buy then settle) ─────────────────────────────────────────

def round_trip_pnl(
    usdc_spent: float,
    ask_price: float,
    outcome: float,            # 1.0 for win, 0.0 for loss
    fee_rate: float = config.TAKER_FEE_RATE,
) -> Dict:
    """
    Given USDC spent (inclusive of fee), compute the net PnL.

    outcome: 1.0 = win (share settles at $1), 0.0 = loss (share settles at $0).
    Fractional outcomes (e.g. 0.5) not supported on binary markets.
    """
    shares = usdc_spent / (ask_price * (1 + fee_rate))
    gross_recv = shares * outcome
    net_pnl    = gross_recv - usdc_spent
    roi        = net_pnl / usdc_spent if usdc_spent > 0 else None

    return {
        "usdc_spent":  round(usdc_spent, 6),
        "ask_price":   ask_price,
        "shares":      round(shares, 6),
        "outcome":     outcome,
        "gross_recv":  round(gross_recv, 6),
        "net_pnl":     round(net_pnl, 6),
        "roi":         round(roi, 6) if roi is not None else None,
        "fee_rate":    fee_rate,
    }


# ── spread cost (taker crosses the spread) ───────────────────────────────────

def spread_cost(
    best_bid: float,
    best_ask: float,
    shares: float,
    fee_rate: float = config.TAKER_FEE_RATE,
) -> Dict:
    """
    If you buy at best_ask and immediately sell at best_bid as a taker,
    what is the round-trip loss (spread + fees)?

    This measures the raw cost of being wrong about direction.
    """
    if best_bid >= best_ask:
        raise ValueError(f"Crossed book: bid={best_bid} >= ask={best_ask}")

    buy_cost  = usdc_for_shares(shares, best_ask, fee_rate)
    sell_recv = compute_taker_sell(best_bid, shares, fee_rate).net_received_usdc
    round_trip_loss = buy_cost - sell_recv

    spread_abs  = best_ask - best_bid
    mid         = (best_bid + best_ask) / 2
    spread_pct  = spread_abs / mid * 100 if mid > 0 else None
    fee_cost    = usdc_for_shares(shares, best_ask, fee_rate) - (shares * best_ask) \
                + (shares * best_bid) - sell_recv  # double-fee load

    return {
        "best_bid":         best_bid,
        "best_ask":         best_ask,
        "shares":           shares,
        "buy_cost_usdc":    round(buy_cost, 6),
        "sell_recv_usdc":   round(sell_recv, 6),
        "round_trip_loss":  round(round_trip_loss, 6),
        "spread_abs":       round(spread_abs, 6),
        "spread_pct":       round(spread_pct, 4) if spread_pct else None,
        "fee_drag_usdc":    round(fee_cost, 6),
        "loss_pct_of_notional": round(round_trip_loss / (shares * mid) * 100, 4) if mid > 0 else None,
    }


# ── edge required to be profitable ───────────────────────────────────────────

def required_edge(
    ask_price: float,
    fee_rate: float = config.TAKER_FEE_RATE,
) -> float:
    """
    The minimum true probability advantage needed for EV > 0 when buying at ask_price.

    required_true_prob = ask_price * (1 + fee_rate)
    edge_needed = required_true_prob - ask_price = ask_price * fee_rate

    So: your true belief must exceed the ask by at least ask_price * fee_rate.
    At 0.50 ask: need 0.50 * 0.02 = 1.0¢ true-prob edge.
    At 0.90 ask: need 0.90 * 0.02 = 1.8¢ true-prob edge.
    """
    return round(ask_price * fee_rate, 8)


def min_true_prob_to_buy(
    ask_price: float,
    fee_rate: float = config.TAKER_FEE_RATE,
) -> float:
    """
    Minimum true win probability at which buying at ask_price has EV ≥ 0.
    = ask_price * (1 + fee_rate)
    """
    return round(ask_price * (1 + fee_rate), 8)


# ── CLI / self-test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("fee_math.py – sanity checks")
    print("=" * 60)

    # Test 1: buy YES at 0.55 for $5 budget
    ask = 0.55
    budget = 5.0
    shares = max_shares_from_usdc(budget, ask)
    cost = compute_taker_buy(ask, shares)
    print(f"\n[BUY YES @ {ask}] budget=${budget}")
    print(f"  shares            = {shares:.4f}")
    print(f"  total_cost_usdc   = {cost.total_cost_usdc:.4f}  (should ≈ {budget:.4f})")
    print(f"  taker_fee_usdc    = {cost.taker_fee_usdc:.4f}")
    print(f"  pnl_if_win        = {cost.pnl_if_win:.4f}")
    print(f"  pnl_if_loss       = {cost.pnl_if_loss:.4f}")
    print(f"  break_even_prob   = {cost.break_even_true_prob:.4f}")
    print(f"  roi_if_win        = {cost.roi_if_win:.4%}")

    # Test 2: round trip loss at tight spread
    print(f"\n[SPREAD COST] bid=0.495 ask=0.505, 10 shares")
    sc = spread_cost(0.495, 0.505, 10)
    for k, v in sc.items():
        print(f"  {k:30s} = {v}")

    # Test 3: min true prob
    for ask in [0.50, 0.60, 0.70, 0.80, 0.90, 0.95]:
        mtp = min_true_prob_to_buy(ask)
        print(f"  ask={ask:.2f}  min_true_prob={mtp:.4f}  edge_needed={required_edge(ask):.4f}")

    # Test 4: min size check with 30 USDC bankroll
    print(f"\n[MIN SIZE CHECK] bankroll={config.BANKROLL_USDC} max_pos={config.MAX_POSITION_USDC}")
    for ask in [0.50, 0.70, 0.90]:
        desired_shares = max_shares_from_usdc(config.MAX_POSITION_USDC, ask)
        chk = check_min_size(
            desired_shares, config.DEFAULT_MIN_SHARES, ask, config.MAX_POSITION_USDC
        )
        print(f"  ask={ask}  {chk}")
