"""
Settlement safety scorer: compute 0-1 score estimating reliability of settlement.

Hard rejects for live candidates:
  - Paris/CDG/Le Bourget cities
  - Precipitation/rainfall markets
  - Unknown/ambiguous settlement source
  - High manipulation risk

Paper mode still logs rejected markets with reason.
"""
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

HARD_REJECT_CITIES = {"paris", "cdg", "le bourget", "le_bourget"}
HARD_REJECT_MARKET_TYPES = {"precipitation"}
HARD_REJECT_RISK_KEYWORDS = {"wunderground", "weather underground"}

RISK_PENALTY_MAP = {
    "wunderground": 0.35,
    "weather underground": 0.35,
    "provisional": 0.10,
    "revised data": 0.15,
    "credible source": 0.10,
    "dispute": 0.20,
    "le bourget": 0.40,
    "cdg": 0.30,
    "precipitation": 0.20,
    "rainfall": 0.15,
    "rain total": 0.15,
    "metar": 0.05,   # small penalty (metar can be fine, just note it)
    "noaa": 0.03,
    "nws": 0.03,
}

RISK_LEVEL_PENALTY = {
    "low": 0.0,
    "medium": 0.08,
    "high": 0.25,
    "unknown": 0.15,
}


@dataclass
class SafetyResult:
    score: float                # 0-1
    hard_reject: bool
    hard_reject_reason: Optional[str]
    penalties_applied: list[str]
    station_risk: str
    source_clarity: str         # "clear" / "partial" / "unclear" / "unknown"
    is_blacklisted: bool
    is_precipitation: bool
    paper_ok: bool              # always True in V1 (paper mode logs everything)


def _score_source_clarity(resolution_source_text: Optional[str]) -> tuple[float, str]:
    """
    Return (bonus, label) based on resolution source clarity.
    Clear, specific sources get a small bonus. Unknown sources get a penalty.
    """
    if not resolution_source_text:
        return -0.15, "unknown"

    src = resolution_source_text.lower()

    # High-clarity sources
    if any(kw in src for kw in ["weather.gov", "aviationweather", "ogimet", "iowa environmental mesonet"]):
        return 0.05, "clear"

    if any(kw in src for kw in ["noaa", "nws", "national weather service"]):
        return 0.02, "partial"

    if any(kw in src for kw in ["wunderground", "weather underground", "pws"]):
        return -0.20, "unclear"

    if any(kw in src for kw in ["credible source", "reliable source", "official source"]):
        return -0.10, "partial"  # vague language = penalty

    # Some mention of source but not specific
    return -0.05, "partial"


def compute_safety(
    city: Optional[str],
    market_type: str,
    resolution_source_text: Optional[str],
    risk_keywords_found: list[str],
    station_risk: str,
    manipulation_flag: bool,
    is_precipitation: bool,
    parse_failed: bool,
    station_known: bool,
) -> SafetyResult:
    """
    Compute settlement safety score.

    Base score: 1.0
    Subtract penalties for each risk factor.
    Hard reject if any hard-reject condition met.
    """
    hard_reject = False
    hard_reject_reason = None
    penalties: list[str] = []

    city_lower = (city or "").lower()

    # Hard reject: blacklisted city
    is_blacklisted = any(bad in city_lower for bad in HARD_REJECT_CITIES)
    if manipulation_flag or is_blacklisted:
        hard_reject = True
        hard_reject_reason = "blacklisted_city_or_manipulation_flag"

    # Hard reject: precipitation market
    if is_precipitation or market_type in HARD_REJECT_MARKET_TYPES:
        hard_reject = True
        hard_reject_reason = (hard_reject_reason or "") + "|precipitation_market"

    # Hard reject: wunderground in risk_keywords or source text
    src_lower = (resolution_source_text or "").lower()
    for kw in HARD_REJECT_RISK_KEYWORDS:
        if kw in [k.lower() for k in risk_keywords_found] or kw in src_lower:
            hard_reject = True
            hard_reject_reason = (hard_reject_reason or "") + f"|hard_reject_source:{kw}"

    # Hard reject: parse failed entirely
    if parse_failed:
        hard_reject = True
        hard_reject_reason = (hard_reject_reason or "") + "|parse_failed"

    # Base score
    score = 1.0

    # Station risk penalty
    risk_pen = RISK_LEVEL_PENALTY.get(station_risk, 0.15)
    if risk_pen > 0:
        score -= risk_pen
        penalties.append(f"station_risk_{station_risk}_{risk_pen:.2f}")

    # Unknown station penalty
    if not station_known:
        score -= 0.10
        penalties.append("station_unknown_0.10")

    # Per-keyword penalties
    kw_lower = [k.lower() for k in risk_keywords_found]
    for kw, pen in RISK_PENALTY_MAP.items():
        if kw in kw_lower:
            score -= pen
            penalties.append(f"kw_{kw}_{pen:.2f}")

    # Source clarity bonus/penalty
    source_bonus, source_clarity = _score_source_clarity(resolution_source_text)
    score += source_bonus
    if source_bonus < 0:
        penalties.append(f"source_clarity_{source_clarity}_{source_bonus:.2f}")

    # Manipulation flag (beyond blacklist)
    if manipulation_flag and not is_blacklisted:
        score -= 0.20
        penalties.append("manipulation_flag_0.20")

    # Clamp to [0, 1]
    score = max(0.0, min(1.0, score))

    # If hard reject, cap score low
    if hard_reject:
        score = min(score, 0.20)

    return SafetyResult(
        score=round(score, 3),
        hard_reject=hard_reject,
        hard_reject_reason=hard_reject_reason.strip("|") if hard_reject_reason else None,
        penalties_applied=penalties,
        station_risk=station_risk,
        source_clarity=source_clarity,
        is_blacklisted=is_blacklisted,
        is_precipitation=is_precipitation,
        paper_ok=True,
    )


def is_candidate_eligible(safety: SafetyResult, min_safety: float = 0.65) -> bool:
    """Return True if market clears the minimum bar for a candidate ghost trade."""
    return not safety.hard_reject and safety.score >= min_safety
