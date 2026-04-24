"""
Parse Polymarket weather market questions into structured fields.

Conservative: if bucket/source cannot be reliably parsed, mark parse_failed.
Never crash.
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

MARKET_TYPE_DAILY_HIGH = "daily_high_temperature"
MARKET_TYPE_DAILY_LOW = "daily_low_temperature"
MARKET_TYPE_PRECIPITATION = "precipitation"
MARKET_TYPE_UNKNOWN = "unknown"

UNIT_F = "F"
UNIT_C = "C"

RISK_KEYWORDS = [
    "wunderground",
    "weather underground",
    "provisional",
    "revised data",
    "credible source",
    "dispute",
    "metar",
    "noaa",
    "nws",
    "le bourget",
    "cdg",
    "precipitation",
    "rainfall",
    "rain total",
]

MANIPULATION_CITIES = {"paris", "cdg", "le bourget"}

# Patterns for temperature ranges in bucket questions
_RANGE_PATTERNS = [
    # "between 85°F and 89°F" / "between 85 and 89 degrees F"
    re.compile(
        r"between\s+(-?\d+(?:\.\d+)?)\s*[°]?\s*([FC])?\s+and\s+(-?\d+(?:\.\d+)?)\s*[°]?\s*([FC])?",
        re.IGNORECASE,
    ),
    # "85°F - 89°F" / "85 - 89 °F"
    re.compile(
        r"(-?\d+(?:\.\d+)?)\s*[°]?\s*([FC])?\s*[-–]\s*(-?\d+(?:\.\d+)?)\s*[°]?\s*([FC])?",
        re.IGNORECASE,
    ),
    # "at least 90°F" / "90°F or above" / "90 or higher"
    re.compile(
        r"(?:at least|>=?|≥|above|or (above|higher|more))\s*(-?\d+(?:\.\d+)?)\s*[°]?\s*([FC])?",
        re.IGNORECASE,
    ),
    # "below 60°F" / "less than 60°F" / "under 60°F"
    re.compile(
        r"(?:below|less than|under|<=?|≤)\s*(-?\d+(?:\.\d+)?)\s*[°]?\s*([FC])?",
        re.IGNORECASE,
    ),
    # "85°F or higher" (already covered but extra)
    re.compile(
        r"(-?\d+(?:\.\d+)?)\s*[°]?\s*([FC])?\s+or\s+(higher|above|more|lower|below|less)",
        re.IGNORECASE,
    ),
]

# Month name to number
_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass
class ParsedMarket:
    market_id: str
    question: str
    # core fields
    city: Optional[str] = None
    date_str: Optional[str] = None
    market_type: str = MARKET_TYPE_UNKNOWN
    unit: Optional[str] = None
    # bucket
    bucket_label: Optional[str] = None
    bucket_low: Optional[float] = None
    bucket_high: Optional[float] = None
    open_ended_low: bool = False   # bucket is "<= X" (no lower bound)
    open_ended_high: bool = False  # bucket is ">= X" (no upper bound)
    exact_boundary: Optional[str] = None  # "inclusive" / "exclusive" / "unknown"
    # settlement
    resolution_source_text: Optional[str] = None
    station_url: Optional[str] = None
    # risk
    risk_keywords_found: list[str] = field(default_factory=list)
    manipulation_flag: bool = False
    parse_failed: bool = False
    parse_failure_reason: Optional[str] = None
    # precipitation flag
    is_precipitation: bool = False


def _normalize_question(q: str) -> str:
    return q.strip().replace("°", "°").replace("–", "-").replace("—", "-")


def _detect_market_type(question: str) -> str:
    q = question.lower()

    if any(kw in q for kw in ["rain", "rainfall", "precipitation", "snow", "snowfall", "inches of rain", "mm of rain"]):
        return MARKET_TYPE_PRECIPITATION

    high_patterns = [
        "daily high", "high temperature", "high temp", "max temp", "maximum temp",
        "highest temp", "peak temp", "will the high", "daily maximum",
    ]
    low_patterns = [
        "daily low", "low temperature", "low temp", "min temp", "minimum temp",
        "lowest temp", "will the low", "daily minimum",
    ]

    for p in high_patterns:
        if p in q:
            return MARKET_TYPE_DAILY_HIGH

    for p in low_patterns:
        if p in q:
            return MARKET_TYPE_DAILY_LOW

    # temperature mentioned but no clear high/low → unknown
    if "temperature" in q or "degrees" in q or "°f" in q or "°c" in q:
        return MARKET_TYPE_UNKNOWN

    return MARKET_TYPE_UNKNOWN


def _detect_unit(question: str) -> Optional[str]:
    q = question.lower()
    if "°f" in q or " f " in q or "fahrenheit" in q or re.search(r"\d°f", q):
        return UNIT_F
    if "°c" in q or " c " in q or "celsius" in q or "centigrade" in q or re.search(r"\d°c", q):
        return UNIT_C
    # Scan for degree symbol followed by F or C
    m = re.search(r"°\s*([FC])", question, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


def _extract_city(question: str) -> Optional[str]:
    """
    Heuristic city extraction.
    Look for 'in <City>' or 'for <City>' patterns, or known city names.
    """
    # "Will the daily high temperature in New York City on ..."
    # "Will the high in Chicago on ..."
    patterns = [
        re.compile(r"\bin\s+([\w\s,'-]+?)\s+(?:on|for|reach|exceed|be|between|above|below|at least)", re.IGNORECASE),
        re.compile(r"\bfor\s+([\w\s,'-]+?)\s+(?:on|reach|exceed|be|between|above|below|at least)", re.IGNORECASE),
        re.compile(r"^(?:will (?:the )?(?:daily )?(?:high|low) (?:temp(?:erature)? )?(?:in|for) )([\w\s,'-]+?)\s", re.IGNORECASE),
    ]
    for p in patterns:
        m = p.search(question)
        if m:
            city = m.group(1).strip().rstrip(",")
            # Filter out common false positives
            if city.lower() not in ("the", "a", "an", "this", "that"):
                return city
    return None


def _extract_date(question: str) -> Optional[str]:
    """Extract date string from question."""
    # "on April 25, 2025" / "on 04/25/2025" / "on 2025-04-25"
    date_patterns = [
        re.compile(r"on\s+(\w+ \d{1,2},?\s*\d{4})", re.IGNORECASE),
        re.compile(r"on\s+(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE),
        re.compile(r"on\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE),
        re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
        re.compile(r"\b(\w+ \d{1,2},\s*\d{4})\b", re.IGNORECASE),
    ]
    for p in date_patterns:
        m = p.search(question)
        if m:
            return m.group(1).strip()
    return None


def _extract_bucket(question: str, unit: Optional[str]) -> dict:
    """
    Returns dict with: bucket_low, bucket_high, open_ended_low, open_ended_high, bucket_label
    """
    result = {
        "bucket_low": None,
        "bucket_high": None,
        "open_ended_low": False,
        "open_ended_high": False,
        "bucket_label": None,
    }

    q = question.strip()
    q_lower = q.lower()

    # Open-ended high: "at least X", "X or higher/above", ">= X"
    m = re.search(
        r"(?:at least|>=?|≥)\s*(-?\d+(?:\.\d+)?)\s*[°]?\s*[FC]?",
        q, re.IGNORECASE
    )
    if m:
        val = float(m.group(1))
        result["bucket_low"] = val
        result["open_ended_high"] = True
        result["bucket_label"] = f">= {val}"
        return result

    m = re.search(
        r"(-?\d+(?:\.\d+)?)\s*[°]?\s*[FC]?\s+or\s+(?:higher|above|more)",
        q, re.IGNORECASE
    )
    if m:
        val = float(m.group(1))
        result["bucket_low"] = val
        result["open_ended_high"] = True
        result["bucket_label"] = f">= {val}"
        return result

    # Open-ended low: "below X", "less than X", "under X", "<= X"
    m = re.search(
        r"(?:below|less than|under|<=?|≤)\s*(-?\d+(?:\.\d+)?)\s*[°]?\s*[FC]?",
        q, re.IGNORECASE
    )
    if m:
        val = float(m.group(1))
        result["bucket_high"] = val
        result["open_ended_low"] = True
        result["bucket_label"] = f"< {val}"
        return result

    m = re.search(
        r"(-?\d+(?:\.\d+)?)\s*[°]?\s*[FC]?\s+or\s+(?:lower|below|less)",
        q, re.IGNORECASE
    )
    if m:
        val = float(m.group(1))
        result["bucket_high"] = val
        result["open_ended_low"] = True
        result["bucket_label"] = f"< {val}"
        return result

    # Range: "between X and Y" or "X - Y"
    m = re.search(
        r"between\s+(-?\d+(?:\.\d+)?)\s*[°]?\s*[FC]?\s+and\s+(-?\d+(?:\.\d+)?)\s*[°]?\s*[FC]?",
        q, re.IGNORECASE,
    )
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        result["bucket_low"] = min(lo, hi)
        result["bucket_high"] = max(lo, hi)
        result["bucket_label"] = f"{min(lo,hi)}-{max(lo,hi)}"
        return result

    m = re.search(
        r"(-?\d+(?:\.\d+)?)\s*[°]?\s*[FC]?\s*[-–]\s*(-?\d+(?:\.\d+)?)\s*[°]?\s*[FC]?",
        q, re.IGNORECASE,
    )
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if abs(hi - lo) <= 30:  # sanity: reasonable bucket range
            result["bucket_low"] = min(lo, hi)
            result["bucket_high"] = max(lo, hi)
            result["bucket_label"] = f"{min(lo,hi)}-{max(lo,hi)}"
            return result

    return result


def _extract_resolution_source(question: str) -> Optional[str]:
    """Find resolution/settlement source text."""
    patterns = [
        re.compile(r"(?:according to|based on|source:|settlement:|resolved by|data from)\s+(.+?)(?:\.|$)", re.IGNORECASE),
        re.compile(r"(?:wunderground|weather underground|noaa|nws|metar|weather\.gov|aviationweather)", re.IGNORECASE),
    ]
    for p in patterns:
        m = p.search(question)
        if m:
            try:
                return m.group(1).strip()
            except IndexError:
                return m.group(0).strip()
    return None


def _extract_station_url(question: str) -> Optional[str]:
    m = re.search(r"https?://\S+", question)
    if m:
        return m.group(0)
    return None


def _find_risk_keywords(question: str) -> list[str]:
    q = question.lower()
    found = []
    for kw in RISK_KEYWORDS:
        if kw.lower() in q:
            found.append(kw)
    return found


def parse_market(market_id: str, question: str, outcomes: Optional[list[str]] = None) -> ParsedMarket:
    """
    Parse a single market question into structured fields.
    Returns ParsedMarket with parse_failed=True if critical fields cannot be determined.
    Never raises.
    """
    try:
        return _parse_market_inner(market_id, question, outcomes)
    except Exception as exc:
        logger.exception("Unexpected parse error for market %s: %s", market_id, exc)
        return ParsedMarket(
            market_id=market_id,
            question=question,
            parse_failed=True,
            parse_failure_reason=f"unexpected_exception: {exc}",
        )


def _parse_market_inner(market_id: str, question: str, outcomes: Optional[list[str]]) -> ParsedMarket:
    q = _normalize_question(question)

    market_type = _detect_market_type(q)
    is_precipitation = market_type == MARKET_TYPE_PRECIPITATION

    unit = _detect_unit(q)

    city = _extract_city(q)
    date_str = _extract_date(q)

    bucket_info = _extract_bucket(q, unit)
    bucket_low = bucket_info["bucket_low"]
    bucket_high = bucket_info["bucket_high"]
    open_ended_low = bucket_info["open_ended_low"]
    open_ended_high = bucket_info["open_ended_high"]
    bucket_label = bucket_info["bucket_label"]

    # Fallback: try to infer bucket from outcomes list
    if bucket_label is None and outcomes:
        for outcome in outcomes:
            b = _extract_bucket(outcome, unit)
            if b["bucket_label"]:
                bucket_low = b["bucket_low"]
                bucket_high = b["bucket_high"]
                open_ended_low = b["open_ended_low"]
                open_ended_high = b["open_ended_high"]
                bucket_label = b["bucket_label"]
                break

    resolution_source = _extract_resolution_source(q)
    station_url = _extract_station_url(q)
    risk_kw = _find_risk_keywords(q)

    manipulation_flag = False
    if city:
        if city.lower() in MANIPULATION_CITIES:
            manipulation_flag = True
    for kw in ["paris", "cdg", "le bourget", "le_bourget"]:
        if kw in q.lower():
            manipulation_flag = True

    # Determine parse failure
    parse_failed = False
    parse_failure_reason = None

    if market_type == MARKET_TYPE_UNKNOWN and not is_precipitation:
        # Still useful if we have temperature keywords; mark as partial
        if "temperature" not in q.lower() and "°" not in q and "degrees" not in q.lower():
            parse_failed = True
            parse_failure_reason = "market_type_undetermined_no_temperature_keywords"

    if unit is None and not is_precipitation and not parse_failed:
        # Many markets don't state unit in the question — allowed; just unknown
        pass

    if bucket_label is None and not is_precipitation:
        parse_failed = True
        parse_failure_reason = (parse_failure_reason or "") + "|bucket_not_parseable"

    # Determine exact boundary hint
    exact_boundary = "unknown"
    q_lower = q.lower()
    if "inclusive" in q_lower or "or equal" in q_lower:
        exact_boundary = "inclusive"
    elif "exclusive" in q_lower or "strictly" in q_lower:
        exact_boundary = "exclusive"

    return ParsedMarket(
        market_id=market_id,
        question=q,
        city=city,
        date_str=date_str,
        market_type=market_type,
        unit=unit,
        bucket_label=bucket_label,
        bucket_low=bucket_low,
        bucket_high=bucket_high,
        open_ended_low=open_ended_low,
        open_ended_high=open_ended_high,
        exact_boundary=exact_boundary,
        resolution_source_text=resolution_source,
        station_url=station_url,
        risk_keywords_found=risk_kw,
        manipulation_flag=manipulation_flag,
        parse_failed=parse_failed,
        parse_failure_reason=parse_failure_reason.strip("|") if parse_failure_reason else None,
        is_precipitation=is_precipitation,
    )


def parse_markets_bulk(markets: list[dict]) -> list[ParsedMarket]:
    """
    Parse a list of dicts with keys: market_id, question, outcomes (optional).
    """
    results = []
    for m in markets:
        mid = str(m.get("market_id") or m.get("id") or "")
        q = str(m.get("question") or "")
        outcomes = m.get("outcomes") or []
        if not mid or not q:
            continue
        results.append(parse_market(mid, q, outcomes))
    return results
