"""
Fee Engine — YENİ formül (30 Mart 2026+)

  fee = C × p × feeRate × (p × (1-p))^exponent

  feeRate = 0.072, exponent = 1  (config'den okunur)
  Peak ~%1.80 @ p=0.50

Örnekler:
  p=0.50 → 1 share × 0.50 × 0.072 × (0.50×0.50)^1 = 0.0090  (%1.80 of cost)
  p=0.90 → 1 share × 0.90 × 0.072 × (0.90×0.10)^1 = 0.005832  (%0.65 of cost)
  p=0.95 → 1 share × 0.95 × 0.072 × (0.95×0.05)^1 = 0.003249  (%0.34 of cost)

Fee buy'da SHARE olarak, sell'de USDC olarak kesilir.
Minimum fee: 0.0001 USDC.  Yuvarlanır: round(..., 4).
"""


def compute_fee(
    shares: float,
    p: float,
    fee_rate: float = 0.072,
    fee_exponent: float = 1.0,
) -> float:
    """
    YENİ fee formülü hesapla.

    Args:
        shares: Alınacak / satılacak share adedi (C)
        p: İşlem fiyatı (0 < p < 1)
        fee_rate: CLOB fee rate (default 0.072)
        fee_exponent: Exponent (default 1)

    Returns:
        Fee miktarı (USDC cinsinden, buy'da share değeri = fee/share_price)
    """
    if not (0.0 < p < 1.0):
        raise ValueError(f"p must be in (0,1), got {p}")
    raw = shares * p * fee_rate * (p * (1.0 - p)) ** fee_exponent
    return max(round(raw, 4), 0.0001)


def compute_fee_pct(
    p: float,
    fee_rate: float = 0.072,
    fee_exponent: float = 1.0,
) -> float:
    """
    1 share için maliyetin yüzdesi olarak fee döndür.
    cost_per_share = p  →  fee_pct = fee / p * 100
    """
    fee = compute_fee(1.0, p, fee_rate, fee_exponent)
    return fee / p * 100.0


def net_pnl(
    shares: float,
    p_entry: float,
    outcome_win: bool,
    fee_rate: float = 0.072,
    fee_exponent: float = 1.0,
) -> float:
    """
    Kaba PnL hesabı (fee dahil).
    Win: payout = shares × 1.0, cost = shares × p_entry, fee on buy side
    Loss: payout = 0
    """
    entry_fee = compute_fee(shares, p_entry, fee_rate, fee_exponent)
    cost = shares * p_entry + entry_fee
    if outcome_win:
        return shares - cost   # payout 1.0 per share
    else:
        return -cost


def is_edge_positive(
    p_entry: float,
    p_true: float,
    shares: float = 1.0,
    fee_rate: float = 0.072,
    fee_exponent: float = 1.0,
    min_edge_pct: float = 1.0,
) -> tuple[bool, float]:
    """
    p_true (signal prob) vs p_entry (market price) + fee.
    Edge = EV/cost - 1  (yüzde olarak)

    Returns (has_edge, edge_pct)
    """
    fee = compute_fee(shares, p_entry, fee_rate, fee_exponent)
    cost = shares * p_entry + fee
    ev = p_true * shares  # expected payout
    edge_pct = (ev / cost - 1.0) * 100.0
    return (edge_pct >= min_edge_pct, edge_pct)
