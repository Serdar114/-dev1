"""
fee_math.py - Polymarket taker fee mathematics.

FEE MODEL UNCERTAINTY (read before trusting any output):
  Polymarket CLOB documentation describes a 2% taker fee on notional.
  Two mechanically distinct implementations exist:

  Model A – "usdc_extra" (assumed here):
    You order N shares at price p.
    You pay:         p * N * (1 + fee_rate)  USDC out of wallet
    You receive:     N shares
    Net shares held: N

  Model B – "share_cut":
    You order N shares at price p.
    You pay:         p * N  USDC
    You receive:     N * (1 - fee_rate) shares
    Net shares held: N * (1 - fee_rate)

  Break-even true probability differs between models:
    Model A: p_break_even = ask * (1 + fee_rate)      e.g. 0.50 * 1.02 = 0.510
    Model B: p_break_even = ask / (1 - fee_rate)       e.g. 0.50 / 0.98 ≈ 0.510

  At 2% fee the numerical difference is small (~0.04pp at mid-price),
  but the gross/net shares breakdown matters for position sizing.

  fee_model_assumption is stamped on every OrderCost.
  Use compute_both_models() to get both side-by-side.

FEE RATE UNCERTAINTY:
  TAKER_FEE_RATE is read from config.TAKER_FEE_RATE (default 0.02 = 2%).
  Use fee_fetcher.session_fee_fetch() to attempt live retrieval.
  On fallback, fee_rate_source = "config_fallback" and fee_truth_status = "assumed".
  These fields are stamped on every OrderCost via the fee_truth_info parameter.

FEE MODEL RESOLUTION:
  fee_model_assumption is always "unresolved" unless fill receipts have been
  examined to confirm which model is in use. Do NOT upgrade it without evidence.
"""

from dataclasses import dataclass, asdict, field
from typing import Dict, Optional
import json

import config


FEE_MODEL = "usdc_extra"   # assumed; change only with empirical fill evidence


# ── core data structure ───────────────────────────────────────────────────────

@dataclass
class OrderCost:
    """
    Full breakdown of a taker order's economics.

    fee_truth_info fields (fee_rate_source, fee_truth_status, fee_model_assumption)
    are set by the caller via fee_truth_info dict from fee_fetcher.
    Defaults are the most conservative (config_fallback, assumed, unresolved).
    """

    # inputs
    side: str                      # "BUY" or "SELL"
    price_per_share: float         # limit price 0 < p < 1
    shares_gross: float            # shares as specified in order
    taker_fee_rate: float          # e.g. 0.02
    fee_model: str = FEE_MODEL     # "usdc_extra" or "share_cut"

    # share decomposition
    fee_shares: float = 0.0        # shares taken as fee (0 under usdc_extra)
    shares_net: float = 0.0        # shares actually received/held

    # USDC decomposition
    notional_usdc: float = 0.0     # price_per_share * shares_gross
    fee_usdc: float = 0.0          # USDC fee charged (0 under share_cut)
    total_cost_usdc: float = 0.0   # net USDC out of wallet (BUY side)
    net_received_usdc: float = 0.0 # net USDC into wallet (SELL side)

    # settlement scenarios (BUY side)
    pnl_if_win: Optional[float] = None    # shares_net * 1.0 - total_cost_usdc
    pnl_if_loss: Optional[float] = None   # -total_cost_usdc

    # break-even and edge
    break_even_true_prob: Optional[float] = None  # min true_prob for EV >= 0
    edge_at_true_prob: Optional[float] = None     # requires caller to supply true_prob
    edge_known: bool = False                       # False if true_prob was not supplied

    # diagnostics
    roi_if_win: Optional[float] = None    # pnl_if_win / total_cost_usdc
    effective_price: Optional[float] = None  # total_cost_usdc / shares_net

    # ── fee truth tracking (set via fee_truth_info from fee_fetcher) ──────────
    token_id: Optional[str] = None
    fee_rate_source: str = "config_fallback"     # "live_endpoint" | "config_fallback"
    fee_truth_status: str = "assumed"            # "confirmed" | "assumed" | "unresolved"
    fee_model_assumption: str = "unresolved"     # "usdc_extra" | "share_cut" | "unresolved"
    fee_fallback_used: bool = True               # True when fee_rate_source != "live_endpoint"
    min_order_size_used: Optional[float] = None  # min_order_size that was enforced

    # backwards compat alias
    @property
    def shares(self) -> float:
        return self.shares_gross

    @property
    def fee_rate_value(self) -> float:
        """Alias for taker_fee_rate for fee truth audit compatibility."""
        return self.taker_fee_rate

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["shares"] = self.shares            # keep compat alias
        d["fee_rate_value"] = self.taker_fee_rate  # audit alias
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


# ── fee truth info helper ─────────────────────────────────────────────────────

def _apply_fee_truth(order: OrderCost, fee_truth_info: Optional[Dict]) -> OrderCost:
    """
    Stamp fee truth tracking fields onto an OrderCost from a fee_truth_info dict
    (as returned by fee_fetcher.fetch_live_fee_rate or session_fee_fetch).
    Returns the same order object (mutated in place).
    """
    if fee_truth_info is None:
        return order
    order.token_id           = fee_truth_info.get("token_id") or order.token_id
    order.fee_rate_source    = fee_truth_info.get("fee_rate_source", "config_fallback")
    order.fee_truth_status   = fee_truth_info.get("fee_truth_status", "assumed")
    order.fee_model_assumption = fee_truth_info.get("fee_model_assumption", "unresolved")
    order.fee_fallback_used  = (order.fee_rate_source != "live_endpoint")
    return order


# ── core computation ──────────────────────────────────────────────────────────

def compute_taker_buy(
    price_per_share: float,
    shares: float,
    true_prob: Optional[float] = None,
    fee_rate: float = config.TAKER_FEE_RATE,
    fee_truth_info: Optional[Dict] = None,
    token_id: Optional[str] = None,
    min_order_size_used: Optional[float] = None,
) -> OrderCost:
    """
    Compute full cost and PnL for a TAKER BUY order under FEE_MODEL='usdc_extra'.

    Args:
        price_per_share    : ask price in USDC per share (0 < p < 1)
        shares             : gross shares ordered (= shares received under this model)
        true_prob          : caller's estimate of true win probability (optional).
                             If None, edge_at_true_prob is None and edge_known=False.
                             DO NOT pass mid-price as true_prob – that is circular.
        fee_rate           : taker fee rate (default from config; use fee_truth_info
                             to propagate a live-fetched rate with provenance)
        fee_truth_info     : dict from fee_fetcher.fetch_live_fee_rate() or
                             session_fee_fetch(). Stamps fee_rate_source,
                             fee_truth_status, fee_model_assumption onto output.
                             If None: fee_rate_source="config_fallback", status="assumed".
        token_id           : token this order is for (stamped on output)
        min_order_size_used: the min_order_size that was enforced for this order

    Returns:
        OrderCost with all fields populated. fee_model='usdc_extra'.
    """
    if not (0 < price_per_share < 1):
        raise ValueError(f"price_per_share must be in (0, 1), got {price_per_share}")
    if shares <= 0:
        raise ValueError(f"shares must be > 0, got {shares}")
    if not (0 <= fee_rate < 1):
        raise ValueError(f"fee_rate must be in [0, 1), got {fee_rate}")

    # Under usdc_extra: you receive all shares you ordered
    shares_net  = shares
    fee_shares  = 0.0
    notional    = price_per_share * shares
    fee_usdc    = notional * fee_rate
    total_cost  = notional + fee_usdc
    eff_price   = total_cost / shares_net  # effective cost per share received

    # Settlement PnL: shares_net pay $1 on win, $0 on loss
    pnl_win  = shares_net * 1.0 - total_cost
    pnl_loss = -total_cost

    # Break-even: EV(p_true) = p_true * 1 * shares_net - total_cost = 0
    #   p_true = total_cost / shares_net = eff_price
    be_prob = eff_price

    edge = None
    edge_known = False
    if true_prob is not None:
        # edge per share = true_prob * 1 - eff_price
        # WARNING: do not set true_prob = market_mid; that is circular.
        edge = true_prob - eff_price
        edge_known = True

    roi = pnl_win / total_cost if total_cost > 0 else None

    order = OrderCost(
        side="BUY",
        price_per_share=price_per_share,
        shares_gross=round(shares, 8),
        taker_fee_rate=fee_rate,
        fee_model=FEE_MODEL,
        fee_shares=0.0,
        shares_net=round(shares_net, 8),
        notional_usdc=round(notional, 8),
        fee_usdc=round(fee_usdc, 8),
        total_cost_usdc=round(total_cost, 8),
        net_received_usdc=0.0,
        pnl_if_win=round(pnl_win, 8),
        pnl_if_loss=round(pnl_loss, 8),
        break_even_true_prob=round(be_prob, 8),
        edge_at_true_prob=round(edge, 8) if edge is not None else None,
        edge_known=edge_known,
        roi_if_win=round(roi, 6) if roi is not None else None,
        effective_price=round(eff_price, 8),
        token_id=token_id,
        min_order_size_used=min_order_size_used,
    )
    return _apply_fee_truth(order, fee_truth_info)


def compute_taker_sell(
    price_per_share: float,
    shares: float,
    fee_rate: float = config.TAKER_FEE_RATE,
) -> OrderCost:
    """
    Compute net USDC received for a TAKER SELL order.
    Under usdc_extra model: fee is deducted from proceeds.
    """
    if not (0 < price_per_share < 1):
        raise ValueError(f"price_per_share must be in (0, 1), got {price_per_share}")
    if shares <= 0:
        raise ValueError(f"shares must be > 0, got {shares}")

    notional   = price_per_share * shares
    fee_usdc   = notional * fee_rate
    net_recv   = notional - fee_usdc

    return OrderCost(
        side="SELL",
        price_per_share=price_per_share,
        shares_gross=round(shares, 8),
        taker_fee_rate=fee_rate,
        fee_model=FEE_MODEL,
        fee_shares=0.0,
        shares_net=round(shares, 8),
        notional_usdc=round(notional, 8),
        fee_usdc=round(fee_usdc, 8),
        total_cost_usdc=0.0,
        net_received_usdc=round(net_recv, 8),
    )


# ── share_cut model ────────────────────────────────────────────────────────────

def compute_taker_buy_share_cut(
    price_per_share: float,
    shares: float,
    true_prob: Optional[float] = None,
    fee_rate: float = config.TAKER_FEE_RATE,
    fee_truth_info: Optional[Dict] = None,
    token_id: Optional[str] = None,
    min_order_size_used: Optional[float] = None,
) -> OrderCost:
    """
    Compute full cost and PnL for a TAKER BUY order under FEE_MODEL='share_cut'.

    Under share_cut:
      You pay:    price_per_share * shares  USDC (no USDC surcharge)
      You receive: shares * (1 - fee_rate)  net shares
      fee_shares:  shares * fee_rate        (deducted from fill)
      fee_usdc:    0.0
    """
    if not (0 < price_per_share < 1):
        raise ValueError(f"price_per_share must be in (0, 1), got {price_per_share}")
    if shares <= 0:
        raise ValueError(f"shares must be > 0, got {shares}")
    if not (0 <= fee_rate < 1):
        raise ValueError(f"fee_rate must be in [0, 1), got {fee_rate}")

    fee_shares  = shares * fee_rate
    shares_net  = shares - fee_shares          # = shares * (1 - fee_rate)
    notional    = price_per_share * shares
    fee_usdc    = 0.0
    total_cost  = notional                     # no USDC surcharge
    eff_price   = total_cost / shares_net      # effective cost per share received

    pnl_win  = shares_net * 1.0 - total_cost
    pnl_loss = -total_cost

    # Break-even: p_true * shares_net = total_cost → p_true = total_cost / shares_net
    be_prob = eff_price

    edge = None
    edge_known = False
    if true_prob is not None:
        edge = true_prob - eff_price
        edge_known = True

    roi = pnl_win / total_cost if total_cost > 0 else None

    order = OrderCost(
        side="BUY",
        price_per_share=price_per_share,
        shares_gross=round(shares, 8),
        taker_fee_rate=fee_rate,
        fee_model="share_cut",
        fee_shares=round(fee_shares, 8),
        shares_net=round(shares_net, 8),
        notional_usdc=round(notional, 8),
        fee_usdc=0.0,
        total_cost_usdc=round(total_cost, 8),
        net_received_usdc=0.0,
        pnl_if_win=round(pnl_win, 8),
        pnl_if_loss=round(pnl_loss, 8),
        break_even_true_prob=round(be_prob, 8),
        edge_at_true_prob=round(edge, 8) if edge is not None else None,
        edge_known=edge_known,
        roi_if_win=round(roi, 6) if roi is not None else None,
        effective_price=round(eff_price, 8),
        token_id=token_id,
        min_order_size_used=min_order_size_used,
    )
    return _apply_fee_truth(order, fee_truth_info)


def compute_both_models(
    price_per_share: float,
    shares: float,
    true_prob: Optional[float] = None,
    fee_rate: float = config.TAKER_FEE_RATE,
    fee_truth_info: Optional[Dict] = None,
    token_id: Optional[str] = None,
    min_order_size_used: Optional[float] = None,
) -> Dict:
    """
    Compute taker buy cost under BOTH fee models and return side-by-side.

    Use this when fee_model_assumption is "unresolved" to expose the full
    range of possible economic outcomes.

    Returns:
      {
        "usdc_extra": OrderCost,       # fee paid as extra USDC
        "share_cut":  OrderCost,       # fee deducted from shares received
        "delta": {                     # difference between models (share_cut - usdc_extra)
          "shares_net_delta":    float,
          "fee_usdc_delta":      float,
          "total_cost_delta":    float,
          "pnl_if_win_delta":    float,
          "break_even_delta":    float,
        },
        "fee_model_assumption": "unresolved",
        "note": "...",
      }
    """
    # Build fee_truth_info copies stamped with explicit model assumption
    ue_info = dict(fee_truth_info) if fee_truth_info else {
        "fee_rate_source": "config_fallback",
        "fee_truth_status": "assumed",
    }
    ue_info["fee_model_assumption"] = "usdc_extra"

    sc_info = dict(ue_info)
    sc_info["fee_model_assumption"] = "share_cut"

    ue = compute_taker_buy(
        price_per_share, shares, true_prob, fee_rate, ue_info, token_id, min_order_size_used
    )
    sc = compute_taker_buy_share_cut(
        price_per_share, shares, true_prob, fee_rate, sc_info, token_id, min_order_size_used
    )

    delta = {
        "shares_net_delta":  round(sc.shares_net   - ue.shares_net, 8),
        "fee_usdc_delta":    round(sc.fee_usdc     - ue.fee_usdc,   8),
        "total_cost_delta":  round(sc.total_cost_usdc - ue.total_cost_usdc, 8),
        "pnl_if_win_delta":  round((sc.pnl_if_win or 0) - (ue.pnl_if_win or 0), 8),
        "break_even_delta":  round(
            (sc.break_even_true_prob or 0) - (ue.break_even_true_prob or 0), 8
        ),
    }

    return {
        "usdc_extra":           ue,
        "share_cut":            sc,
        "delta":                delta,
        "fee_model_assumption": "unresolved",
        "note": (
            "fee_model_assumption=unresolved: both outputs shown. "
            "Cannot determine which model applies without examining fill receipts."
        ),
    }


# ── bankroll-constrained position sizing ─────────────────────────────────────

def max_shares_from_usdc(
    usdc_budget: float,
    ask_price: float,
    fee_rate: float = config.TAKER_FEE_RATE,
) -> float:
    """
    Given a USDC budget and an ask price, compute gross shares purchasable.
    Under usdc_extra model: gross == net shares.

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
    """Total USDC out of wallet (including fee) to buy gross `shares`."""
    return shares * ask_price * (1 + fee_rate)


# ── min_order_size constraint check ──────────────────────────────────────────

def check_min_size(
    shares: float,
    min_shares: float,
    ask_price: float,
    usdc_budget: float,
    fee_rate: float = config.TAKER_FEE_RATE,
    min_size_source: str = "default",  # "market_data" or "default" – for logging
) -> Dict:
    """
    Check whether a desired share quantity clears the min_order_size constraint.

    min_size_source should be "market_data" when min_shares came from the API,
    "default" when it fell back to config.DEFAULT_MIN_SHARES.
    Callers must pass the correct source label.
    """
    shares_floored = max(shares, min_shares)
    cost = usdc_for_shares(shares_floored, ask_price, fee_rate)
    budget_ok   = cost <= usdc_budget
    min_size_ok = shares >= min_shares

    viable = budget_ok and min_size_ok

    reasons = []
    if not min_size_ok:
        reasons.append(f"shares {shares:.4f} < min_order_size {min_shares:.4f} (source={min_size_source})")
    if not budget_ok:
        reasons.append(f"cost {cost:.4f} USDC > budget {usdc_budget:.4f} USDC")

    return {
        "viable":           viable,
        "reason":           "; ".join(reasons) if reasons else "ok",
        "shares_wanted":    round(shares, 6),
        "shares_floored":   round(shares_floored, 6),
        "cost_usdc":        round(cost, 6),
        "budget_ok":        budget_ok,
        "min_size_ok":      min_size_ok,
        "min_size_source":  min_size_source,
    }


# ── round-trip PnL ────────────────────────────────────────────────────────────

def round_trip_pnl(
    usdc_spent: float,
    ask_price: float,
    outcome: float,
    fee_rate: float = config.TAKER_FEE_RATE,
) -> Dict:
    """
    Given total USDC spent (inclusive of fee), compute net PnL at settlement.
    outcome: 1.0 = win, 0.0 = loss. Fractional outcomes not modelled.
    """
    shares_net = usdc_spent / (ask_price * (1 + fee_rate))
    gross_recv = shares_net * outcome
    net_pnl    = gross_recv - usdc_spent
    roi        = net_pnl / usdc_spent if usdc_spent > 0 else None

    return {
        "usdc_spent":   round(usdc_spent, 6),
        "ask_price":    ask_price,
        "shares_net":   round(shares_net, 6),
        "outcome":      outcome,
        "gross_recv":   round(gross_recv, 6),
        "net_pnl":      round(net_pnl, 6),
        "roi":          round(roi, 6) if roi is not None else None,
        "fee_rate":     fee_rate,
        "fee_model":    FEE_MODEL,
    }


# ── spread cost ───────────────────────────────────────────────────────────────

def spread_cost(
    best_bid: float,
    best_ask: float,
    shares: float,
    fee_rate: float = config.TAKER_FEE_RATE,
) -> Dict:
    """
    Round-trip loss if you buy at best_ask and immediately sell at best_bid as taker.
    Measures minimum cost of being wrong about direction.
    """
    if best_bid >= best_ask:
        raise ValueError(f"Crossed book: bid={best_bid} >= ask={best_ask}")

    buy_cost  = usdc_for_shares(shares, best_ask, fee_rate)
    sell_recv = compute_taker_sell(best_bid, shares, fee_rate).net_received_usdc
    rt_loss   = buy_cost - sell_recv

    spread_abs = best_ask - best_bid
    mid        = (best_bid + best_ask) / 2
    spread_pct = spread_abs / mid * 100 if mid > 0 else None

    # fee drag = total fees on both legs
    fee_buy  = shares * best_ask * fee_rate
    fee_sell = shares * best_bid * fee_rate
    fee_drag = fee_buy + fee_sell

    return {
        "best_bid":              best_bid,
        "best_ask":              best_ask,
        "shares":                shares,
        "buy_cost_usdc":         round(buy_cost, 6),
        "sell_recv_usdc":        round(sell_recv, 6),
        "round_trip_loss_usdc":  round(rt_loss, 6),
        "spread_abs":            round(spread_abs, 6),
        "spread_pct":            round(spread_pct, 4) if spread_pct else None,
        "fee_drag_usdc":         round(fee_drag, 6),
        "loss_pct_of_notional":  round(rt_loss / (shares * mid) * 100, 4) if mid > 0 else None,
        "fee_model":             FEE_MODEL,
    }


# ── edge helpers ──────────────────────────────────────────────────────────────

def required_edge(ask_price: float, fee_rate: float = config.TAKER_FEE_RATE) -> float:
    """
    Minimum true-prob edge needed beyond the ask to break even as taker.
    = ask_price * fee_rate  (under usdc_extra model)
    """
    return round(ask_price * fee_rate, 8)


def min_true_prob_to_buy(ask_price: float, fee_rate: float = config.TAKER_FEE_RATE) -> float:
    """
    Minimum true win probability at which buying at ask_price has EV >= 0.
    = ask_price * (1 + fee_rate)  (under usdc_extra model)
    """
    return round(ask_price * (1 + fee_rate), 8)


# ── self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    FAIL = 0

    def check(label, got, expected, tol=1e-6):
        global FAIL
        if abs(got - expected) > tol:
            print(f"  FAIL {label}: got {got}, expected {expected}")
            FAIL += 1
        else:
            print(f"  ok   {label}: {got}")

    print("=" * 64)
    print(f"fee_math self-test  (model={FEE_MODEL}  fee_rate={config.TAKER_FEE_RATE})")
    print("=" * 64)

    # ── test representative prices ────────────────────────────────────────────
    # At each price: verify total_cost = shares * ask * (1+fee), break_even, PnL
    for ask in [0.10, 0.25, 0.50, 0.75, 0.90]:
        budget = config.MAX_POSITION_USDC  # $5
        shares = max_shares_from_usdc(budget, ask)
        o = compute_taker_buy(ask, shares)

        print(f"\n[BUY @ ask={ask}  budget=${budget}]")
        print(f"  shares_gross    = {o.shares_gross:.6f}")
        print(f"  shares_net      = {o.shares_net:.6f}  (== gross under usdc_extra)")
        print(f"  fee_shares      = {o.fee_shares:.6f}  (0 under usdc_extra)")
        print(f"  fee_usdc        = {o.fee_usdc:.6f}")
        print(f"  total_cost_usdc = {o.total_cost_usdc:.6f}  (should ≈ {budget})")
        print(f"  effective_price = {o.effective_price:.6f}  (break_even_prob)")
        print(f"  break_even_prob = {o.break_even_true_prob:.6f}")
        print(f"  pnl_if_win      = {o.pnl_if_win:.6f}")
        print(f"  pnl_if_loss     = {o.pnl_if_loss:.6f}")
        print(f"  roi_if_win      = {o.roi_if_win:.4%}")
        print(f"  edge_known      = {o.edge_known}  (no true_prob supplied)")

        # Invariants
        check(f"total_cost≈budget @{ask}",
              o.total_cost_usdc, budget, tol=1e-4)
        check(f"break_even==eff_price @{ask}",
              o.break_even_true_prob, o.effective_price, tol=1e-8)
        check(f"pnl_win + pnl_loss == shares_net - 2*cost @{ask}",
              o.pnl_if_win + o.pnl_if_loss,
              o.shares_net - 2 * o.total_cost_usdc, tol=1e-6)
        check(f"fee_shares==0 under usdc_extra @{ask}",
              o.fee_shares, 0.0)

    # ── edge_known=False when true_prob not supplied ───────────────────────────
    print("\n[edge_known gate]")
    o_no_prob = compute_taker_buy(0.50, 10.0, true_prob=None)
    assert o_no_prob.edge_at_true_prob is None, "edge should be None when true_prob=None"
    assert not o_no_prob.edge_known, "edge_known should be False"
    print("  ok   edge_at_true_prob=None when true_prob=None")
    print("  ok   edge_known=False when true_prob=None")

    # ── circular true_prob=mid would always give negative edge ────────────────
    print("\n[circular edge demonstration – why true_prob=mid is wrong]")
    mid = 0.495
    ask = 0.505
    o_circ = compute_taker_buy(ask, 10.0, true_prob=mid)
    # mid < ask < ask*(1+fee), so edge < 0 always
    print(f"  true_prob=mid={mid}  ask={ask}  edge={o_circ.edge_at_true_prob:.6f}")
    print(f"  → always negative; EV gate using true_prob=mid will ALWAYS fail")
    assert o_circ.edge_at_true_prob < 0, "circular edge should be negative"
    print("  ok   circular edge is negative as expected")

    # ── spread cost ────────────────────────────────────────────────────────────
    print("\n[spread_cost bid=0.495 ask=0.505 shares=10]")
    sc = spread_cost(0.495, 0.505, 10)
    for k, v in sc.items():
        print(f"  {k:30s} = {v}")

    # ── min true prob table ────────────────────────────────────────────────────
    print("\n[min_true_prob_to_buy table]")
    for ask in [0.10, 0.25, 0.50, 0.75, 0.90, 0.95]:
        mtp = min_true_prob_to_buy(ask)
        edge_needed = required_edge(ask)
        print(f"  ask={ask:.2f}  min_true_prob={mtp:.4f}  edge_needed={edge_needed:.4f}")

    # ── check_min_size with source label ──────────────────────────────────────
    print(f"\n[check_min_size  bankroll={config.BANKROLL_USDC}  max_pos={config.MAX_POSITION_USDC}]")
    for ask, src in [(0.50, "market_data"), (0.90, "default")]:
        s = max_shares_from_usdc(config.MAX_POSITION_USDC, ask)
        chk = check_min_size(s, config.DEFAULT_MIN_SHARES, ask,
                              config.MAX_POSITION_USDC, min_size_source=src)
        print(f"  ask={ask}  {chk}")

    print()
    if FAIL:
        print(f"SELF-TEST FAILED: {FAIL} assertion(s) failed")
        sys.exit(1)
    else:
        print("SELF-TEST PASSED")
