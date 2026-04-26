"""
Settlement safety scorer: compute 0-1 score + paper/live eligibility flags.

Three-tier output:
  hard_blacklist  — never log, never trade (Paris/CDG/Le Bourget)
  live_eligible   — safe enough to consider real money entry
  paper_eligible  — can be ghost-logged for observation

Rules:
  - Paris/CDG/Le Bourget:          hard_blacklist=True, paper_eligible=False
  - Precipitation/rainfall:        paper_eligible=True (log only), live_eligible=False
  - Wunderground:                  paper_eligible=True, live_eligible=False
  - Unknown source + unknown city: paper_eligible=True if include_unknown, live_eligible=False
  - Parse failed:                  paper_eligible=True (log the failure), live_eligible=False
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

HARD_BLACKLIST_CITIES = {"paris", "cdg", "le bourget", "le_bourget"}
PRECIPITATION_MARKET_TYPES = {"precipitation"}

# These sources make a market live-ineligible (but paper-OK)
LIVE_REJECT_SOURCES = {"wunderground", "weather underground"}

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
    "metar": 0.05,
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
    score: float                    # 0-1 settlement reliability score
    hard_blacklist: bool            # true = never log, never trade
    paper_eligible: bool            # true = log as observation / ghost
    live_eligible: bool             # true = would consider for real money (V2+)
    reject_reason: Optional[str]    # primary rejection reason string
    live_reject_reasons: list[str] = field(default_factory=list)
    penalties_applied: list[str] = field(default_factory=list)
    station_risk: str = "unknown"
    source_clarity: str = "unknown"
    is_blacklisted: bool = False
    is_precipitation: bool = False
    source_type: str = "Unknown"

    # Legacy compat
    @property
    def hard_reject(self) -> bool:
        return self.hard_blacklist

    @property
    def paper_ok(self) -> bool:
        return self.paper_eligible


def _score_source_clarity(resolution_source_text: Optional[str]) -> tuple[float, str]:
    if not resolution_source_text:
        return -0.15, "unknown"

    src = resolution_source_text.lower()

    if any(kw in src for kw in ["weather.gov", "aviationweather", "ogimet", "iowa environmental mesonet"]):
        return 0.05, "clear"

    if any(kw in src for kw in ["noaa", "nws", "national weather service"]):
        return 0.02, "partial"

    if any(kw in src for kw in ["wunderground", "weather underground", "pws"]):
        return -0.20, "unclear"

    if any(kw in src for kw in ["credible source", "reliable source", "official source"]):
        return -0.10, "partial"

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
    source_type: str = "Unknown",
    include_unknown: bool = True,
) -> SafetyResult:
    """
    Compute settlement safety with three-tier eligibility output.
    """
    city_lower = (city or "").lower()
    src_lower = (resolution_source_text or "").lower()
    kw_lower = [k.lower() for k in risk_keywords_found]

    # ── Hard blacklist check ──────────────────────────────────────────────────
    is_blacklisted = any(bad in city_lower for bad in HARD_BLACKLIST_CITIES)
    if manipulation_flag:
        is_blacklisted = True

    hard_blacklist = is_blacklisted
    hard_blacklist_reason = "blacklisted_city_or_manipulation" if hard_blacklist else None

    # ── Precipitation: paper only ─────────────────────────────────────────────
    is_precip = is_precipitation or market_type in PRECIPITATION_MARKET_TYPES

    # ── Live-reject sources ───────────────────────────────────────────────────
    live_reject_reasons: list[str] = []

    if is_precip:
        live_reject_reasons.append("precipitation_market")

    for kw in LIVE_REJECT_SOURCES:
        if kw in kw_lower or kw in src_lower:
            live_reject_reasons.append(f"live_reject_source:{kw}")

    if source_type in ("Wunderground",):
        if "live_reject_source:wunderground" not in live_reject_reasons:
            live_reject_reasons.append("live_reject_source_type:Wunderground")

    if parse_failed:
        live_reject_reasons.append("parse_failed")

    if not station_known:
        live_reject_reasons.append("station_unknown")

    if source_type == "Unknown" and not station_known:
        live_reject_reasons.append("source_and_station_unknown")

    # ── Eligibility ───────────────────────────────────────────────────────────
    if hard_blacklist:
        paper_eligible = False
        live_eligible = False
        reject_reason = hard_blacklist_reason
    elif is_precip:
        paper_eligible = True   # log for observation
        live_eligible = False
        reject_reason = "precipitation_paper_only"
    elif live_reject_reasons and not parse_failed:
        paper_eligible = True
        live_eligible = False
        reject_reason = "; ".join(live_reject_reasons)
    elif parse_failed:
        paper_eligible = True   # log the failure
        live_eligible = False
        reject_reason = "parse_failed"
    elif not station_known and not include_unknown:
        paper_eligible = False
        live_eligible = False
        reject_reason = "unknown_city_excluded"
    else:
        paper_eligible = True
        live_eligible = len(live_reject_reasons) == 0
        reject_reason = "; ".join(live_reject_reasons) if live_reject_reasons else None

    # ── Score computation ─────────────────────────────────────────────────────
    penalties: list[str] = []
    score = 1.0

    risk_pen = RISK_LEVEL_PENALTY.get(station_risk, 0.15)
    if risk_pen > 0:
        score -= risk_pen
        penalties.append(f"station_risk_{station_risk}_{risk_pen:.2f}")

    if not station_known:
        score -= 0.10
        penalties.append("station_unknown_0.10")

    for kw, pen in RISK_PENALTY_MAP.items():
        if kw in kw_lower:
            score -= pen
            penalties.append(f"kw_{kw}_{pen:.2f}")

    source_bonus, source_clarity = _score_source_clarity(resolution_source_text)
    score += source_bonus
    if source_bonus < 0:
        penalties.append(f"source_clarity_{source_clarity}_{source_bonus:.2f}")

    if manipulation_flag and not is_blacklisted:
        score -= 0.20
        penalties.append("manipulation_flag_0.20")

    score = max(0.0, min(1.0, score))

    if hard_blacklist:
        score = min(score, 0.10)
    elif not live_eligible and live_reject_reasons:
        score = min(score, 0.55)

    return SafetyResult(
        score=round(score, 3),
        hard_blacklist=hard_blacklist,
        paper_eligible=paper_eligible,
        live_eligible=live_eligible,
        reject_reason=reject_reason,
        live_reject_reasons=live_reject_reasons,
        penalties_applied=penalties,
        station_risk=station_risk,
        source_clarity=source_clarity,
        is_blacklisted=is_blacklisted,
        is_precipitation=is_precip,
        source_type=source_type,
    )


def is_candidate_eligible(safety: SafetyResult, min_safety: float = 0.65) -> bool:
    """Return True if market passes the minimum bar for a candidate ghost trade."""
    return safety.paper_eligible and not safety.hard_blacklist and safety.score >= min_safety


def is_live_eligible(safety: SafetyResult, min_safety: float = 0.65) -> bool:
    """Return True if market would qualify for live trading (V2 check)."""
    return safety.live_eligible and safety.score >= min_safety
