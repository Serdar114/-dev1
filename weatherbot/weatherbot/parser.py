"""
Parse Polymarket weather market questions into structured fields.

Conservative: if bucket/source cannot be reliably parsed, mark parse_failed.
Combines question + title + description + rules for richer parsing.
Never crash.
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

MARKET_TYPE_DAILY_HIGH = "daily_high_temperature"
MARKET_TYPE_DAILY_LOW = "daily_low_temperature"
MARKET_TYPE_PRECIPITATION = "precipitation"
MARKET_TYPE_UNKNOWN = "unknown"

UNIT_F = "F"
UNIT_C = "C"

# Source type classification
SOURCE_TYPE_NOAA = "NOAA"
SOURCE_TYPE_NWS = "NWS"
SOURCE_TYPE_METAR = "METAR"
SOURCE_TYPE_WUNDERGROUND = "Wunderground"
SOURCE_TYPE_HKO = "HKO"
SOURCE_TYPE_METOFFICE = "MetOffice"
SOURCE_TYPE_AVIATIONWEATHER = "AviationWeather"
SOURCE_TYPE_UNKNOWN = "Unknown"

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

# ICAO codes known from stations.yaml — accepted from anywhere in combined text
_KNOWN_ICAO_CODES: frozenset[str] = frozenset([
    "KLGA", "KORD", "KDAL", "KDFW", "KMIA", "KLAX", "KATL",
    "RJTT", "RKSI", "WSSS", "LTFM", "ZSPD", "VHHH", "EGLC",
    "LFPB", "LFPG", "EDDB", "EHAM", "YSSY", "YMML", "CYYZ",
    "CYVR", "KPHX", "KIAH", "KDEN", "KSEA", "KMSP", "KBOS",
    "KPHL", "KLAS", "OMDB", "VABB", "MMMX", "SBGR", "SAEZ",
])

# Common English words that happen to match ICAO prefix rules — must be rejected
_ICAO_DENYLIST: frozenset[str] = frozenset([
    # User-required
    "WILL", "HIGH", "TEMP", "DATE", "CITY", "THIS", "THAT", "OVER", "LESS", "MORE",
    # Common W-words (W is a valid ICAO prefix letter)
    "WAIT", "WALK", "WARM", "WASH", "WAVE", "WEEK", "WELL", "WERE", "WHAT", "WHEN",
    "WIDE", "WIND", "WISE", "WISH", "WITH", "WORD", "WORK", "WANT", "WENT",
    # Common L-words
    "LACK", "LAND", "LAST", "LEAD", "LEFT", "LIFE", "LIKE", "LIVE", "LOAD",
    "LONG", "LOOK", "LOSE", "LOVE",
    # Common K-words
    "KEEP", "KNOW",
    # Common C-words
    "CALL", "CAME", "CARE", "CASE", "COLD", "COME", "COOL", "COST",
    # Common B-words
    "BACK", "BALL", "BASE", "BEEN", "BEST", "BOTH", "BURN",
    # Common E-words
    "EACH", "EARN", "EAST", "EASY", "EVEN", "EVER",
    # Common P-words
    "PACK", "PAGE", "PAID", "PAIN", "PART", "PASS", "PAST", "PICK", "PLAN", "PLAY",
    "PLUS", "POOR", "PULL", "PUSH",
    # Common R-words
    "RACE", "RAIN", "RANK", "RATE", "READ", "REAL", "RIDE", "RING", "RISE", "RISK",
    "ROAD", "ROLE", "ROOM", "RULE", "RUSH",
    # Common V-words
    "VERY", "VIEW", "VOTE",
    # Common Y-words
    "YEAR", "YOUR",
    # Common Z-words
    "ZONE",
])

# Keywords that signal station context — ICAO accepted when one is nearby in the text
_STATION_CONTEXT_KWS: tuple[str, ...] = (
    "station", "airport", "metar", "icao", "weather station",
    "observed at", "reported by", "reporting station",
)

# Month name to number
_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_CURRENT_YEAR = datetime.now().year


def _parse_reference_date(close_time_str: Optional[str]) -> Optional[date]:
    """Parse a close_time ISO string to a date, used only for year inference."""
    if not close_time_str:
        return None
    # datetime.fromisoformat handles most ISO formats; normalise Z suffix first
    try:
        return datetime.fromisoformat(close_time_str.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(close_time_str, fmt).date()
        except ValueError:
            pass
    return None


def _infer_year(month: int, day: int, reference_date: Optional[date] = None) -> int:
    """
    Infer the calendar year for a partial date (month + day).

    When reference_date (from close_time) is provided:
      - prefer the year that places the date <= reference_date
      - if ref_year gives a date after reference_date, fall back to ref_year - 1

    When no reference_date:
      - use today's year
      - if result would be > 180 days in the past, roll forward to next year
    """
    today = date.today()
    if reference_date is not None:
        ref_year = reference_date.year
        try:
            candidate = date(ref_year, month, day)
        except ValueError:
            return ref_year
        if candidate <= reference_date:
            return ref_year
        # candidate falls after close_time → must be previous year's occurrence
        try:
            date(ref_year - 1, month, day)  # validate it's a real date
        except ValueError:
            return ref_year
        return ref_year - 1
    else:
        ref_year = today.year
        try:
            candidate = date(ref_year, month, day)
        except ValueError:
            return ref_year
        # More than 6 months in the past → roll forward
        if (today - candidate).days > 180:
            return ref_year + 1
        return ref_year


@dataclass
class ParsedMarket:
    market_id: str
    question: str
    # combined source text used for parsing
    parsed_source_text: str = ""
    parsed_source_confidence: str = "low"  # "high" / "medium" / "low"
    # core fields
    city: Optional[str] = None
    date_str: Optional[str] = None
    parsed_target_date: Optional[date] = None   # <-- KEY: actual weather target date
    market_type: str = MARKET_TYPE_UNKNOWN
    unit: Optional[str] = None
    # bucket
    bucket_label: Optional[str] = None
    bucket_low: Optional[float] = None
    bucket_high: Optional[float] = None
    open_ended_low: bool = False
    open_ended_high: bool = False
    exact_boundary: Optional[str] = None
    # settlement source
    resolution_source_text: Optional[str] = None
    source_type: str = SOURCE_TYPE_UNKNOWN
    station_code_from_text: Optional[str] = None  # ICAO extracted from rules text
    station_url: Optional[str] = None
    # risk
    risk_keywords_found: list[str] = field(default_factory=list)
    manipulation_flag: bool = False
    parse_failed: bool = False
    parse_failure_reason: Optional[str] = None
    # type flags
    is_precipitation: bool = False
    # forecast blocking
    forecast_blocked_reason: Optional[str] = None


def _normalize(text: str) -> str:
    return text.strip().replace("°", "°").replace("–", "-").replace("—", "-")


def _combine_text(
    question: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    rules: Optional[str] = None,
    resolution_text: Optional[str] = None,
) -> tuple[str, str]:
    """
    Combine all available text for parsing. Returns (combined_text, confidence).
    More sources = higher confidence.
    """
    parts = []
    source_count = 0

    if question:
        parts.append(_normalize(question))
        source_count += 1
    if title and title.lower() != question.lower():
        parts.append(_normalize(title))
        source_count += 1
    if description:
        parts.append(_normalize(description))
        source_count += 1
    if rules:
        parts.append(_normalize(rules))
        source_count += 1
    if resolution_text:
        parts.append(_normalize(resolution_text))
        source_count += 1

    combined = " | ".join(p for p in parts if p)

    if source_count >= 3:
        confidence = "high"
    elif source_count == 2:
        confidence = "medium"
    else:
        confidence = "low"

    return combined, confidence


def _detect_market_type(text: str) -> str:
    q = text.lower()

    if any(kw in q for kw in [
        "rain", "rainfall", "precipitation", "snow", "snowfall",
        "inches of rain", "mm of rain", "inches of precip",
    ]):
        return MARKET_TYPE_PRECIPITATION

    high_patterns = [
        "daily high", "high temperature", "high temp", "max temp", "maximum temp",
        "highest temp", "peak temp", "will the high", "daily maximum",
        "high of ", "high will", "maximum temperature",
    ]
    low_patterns = [
        "daily low", "low temperature", "low temp", "min temp", "minimum temp",
        "lowest temp", "will the low", "daily minimum", "low of ",
        "minimum temperature",
    ]

    for p in high_patterns:
        if p in q:
            return MARKET_TYPE_DAILY_HIGH

    for p in low_patterns:
        if p in q:
            return MARKET_TYPE_DAILY_LOW

    if "temperature" in q or "degrees" in q or "°f" in q or "°c" in q:
        return MARKET_TYPE_UNKNOWN

    return MARKET_TYPE_UNKNOWN


def _detect_unit(text: str) -> Optional[str]:
    q = text.lower()
    if "°f" in q or "fahrenheit" in q or re.search(r"\d°f", q):
        return UNIT_F
    if "°c" in q or "celsius" in q or "centigrade" in q or re.search(r"\d°c", q):
        return UNIT_C
    m = re.search(r"°\s*([FC])", text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    # Check for "degrees F" / "degrees C"
    m = re.search(r"degrees?\s+([FC])\b", text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


def _extract_city(text: str) -> Optional[str]:
    """Heuristic city extraction from combined text."""
    patterns = [
        re.compile(
            r"\bin\s+([\w\s,\'\-]+?)\s+(?:on|for|reach|exceed|be|between|above|below|at least|will)",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bfor\s+([\w\s,\'\-]+?)\s+(?:on|reach|exceed|be|between|above|below|at least)",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:will (?:the )?(?:daily )?(?:high|low) (?:temp(?:erature)? )?(?:in|for) )([\w\s,\'\-]+?)\s",
            re.IGNORECASE,
        ),
    ]
    for p in patterns:
        m = p.search(text)
        if m:
            city = m.group(1).strip().rstrip(",").strip()
            if city.lower() not in ("the", "a", "an", "this", "that", "it"):
                # Exclude if city looks like a date or number
                if not re.match(r"^\d", city):
                    return city
    return None


def _parse_date_string(date_str: str) -> Optional[date]:
    """
    Convert a date string like 'April 25, 2025' or '2025-04-25' to a date object.
    Returns None if unparseable.
    """
    if not date_str:
        return None

    date_str = date_str.strip()

    # ISO format
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", date_str)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # "Month DD, YYYY" or "Month DD YYYY"
    m = re.match(
        r"^(\w+)\s+(\d{1,2}),?\s*(\d{4})$", date_str, re.IGNORECASE
    )
    if m:
        month_name = m.group(1).lower()
        month_num = _MONTH_MAP.get(month_name)
        if month_num:
            try:
                return date(int(m.group(3)), month_num, int(m.group(2)))
            except ValueError:
                pass

    # "MM/DD/YYYY"
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", date_str)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass

    # "DD Month YYYY"
    m = re.match(r"^(\d{1,2})\s+(\w+)\s+(\d{4})$", date_str, re.IGNORECASE)
    if m:
        month_name = m.group(2).lower()
        month_num = _MONTH_MAP.get(month_name)
        if month_num:
            try:
                return date(int(m.group(3)), month_num, int(m.group(1)))
            except ValueError:
                pass

    return None


_MONTH_RE = (
    r"(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)"
)


def _extract_date(
    text: str,
    reference_date: Optional[date] = None,
) -> tuple[Optional[str], Optional[date]]:
    """
    Extract the weather target date from market text.

    Tries full-year patterns first; falls back to partial (month + day) patterns
    with year inferred via _infer_year(reference_date).

    Returns (date_str, parsed_date).  date_str is the raw matched string.
    """
    # ── Full-year patterns (no inference needed) ───────────────────────────────
    full_patterns = [
        re.compile(r"on\s+(\w+ \d{1,2},?\s*\d{4})", re.IGNORECASE),
        re.compile(r"on\s+(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE),
        re.compile(r"on\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE),
        re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
        re.compile(r"\b(\w+ \d{1,2},\s*\d{4})\b", re.IGNORECASE),
        re.compile(r"\b(\d{1,2}\s+\w+\s+\d{4})\b", re.IGNORECASE),
    ]
    for p in full_patterns:
        m = p.search(text)
        if m:
            ds = m.group(1).strip()
            parsed = _parse_date_string(ds)
            if parsed:
                return ds, parsed
            return ds, None

    # ── Partial-date patterns (month + day, no year) ───────────────────────────
    # Ordered most-specific first: "on Month DD" beats plain "Month DD"
    partial_patterns = [
        # "on April 26" / "on Apr 26"
        re.compile(rf"on\s+({_MONTH_RE})\s+(\d{{1,2}})\b", re.IGNORECASE),
        # "April 26" / "Apr 26"
        re.compile(rf"\b({_MONTH_RE})\s+(\d{{1,2}})\b", re.IGNORECASE),
        # "26 April" / "26 Apr"
        re.compile(rf"\b(\d{{1,2}})\s+({_MONTH_RE})\b", re.IGNORECASE),
    ]
    for i, p in enumerate(partial_patterns):
        m = p.search(text)
        if not m:
            continue
        if i <= 1:  # Month DD
            month_str, day_str = m.group(1), m.group(2)
        else:  # DD Month
            day_str, month_str = m.group(1), m.group(2)
        month_num = _MONTH_MAP.get(month_str.lower())
        if not month_num:
            continue
        try:
            day_num = int(day_str)
            year = _infer_year(month_num, day_num, reference_date)
            inferred = date(year, month_num, day_num)
            return f"{month_str.capitalize()} {day_num}", inferred
        except ValueError:
            continue

    return None, None


def _extract_bucket(text: str, unit: Optional[str]) -> dict:
    result = {
        "bucket_low": None,
        "bucket_high": None,
        "open_ended_low": False,
        "open_ended_high": False,
        "bucket_label": None,
    }

    q = text.strip()

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

    # Range: "between X and Y"
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

    # Range: "X - Y degrees"
    m = re.search(
        r"(-?\d+(?:\.\d+)?)\s*[°]?\s*[FC]?\s*[-–]\s*(-?\d+(?:\.\d+)?)\s*[°]?\s*[FC]?",
        q, re.IGNORECASE,
    )
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if abs(hi - lo) <= 30:
            result["bucket_low"] = min(lo, hi)
            result["bucket_high"] = max(lo, hi)
            result["bucket_label"] = f"{min(lo,hi)}-{max(lo,hi)}"
            return result

    return result


def _classify_source_type(text: str) -> str:
    """Identify resolution source type from combined text."""
    t = text.lower()
    if "wunderground" in t or "weather underground" in t:
        return SOURCE_TYPE_WUNDERGROUND
    if "aviationweather" in t or "aviation weather" in t:
        return SOURCE_TYPE_AVIATIONWEATHER
    if "metar" in t:
        return SOURCE_TYPE_METAR
    if "hko" in t or "hong kong observatory" in t:
        return SOURCE_TYPE_HKO
    if "met office" in t or "metoffice" in t:
        return SOURCE_TYPE_METOFFICE
    if "national weather service" in t or "nws" in t:
        return SOURCE_TYPE_NWS
    if "noaa" in t:
        return SOURCE_TYPE_NOAA
    return SOURCE_TYPE_UNKNOWN


_ICAO_PATTERN = re.compile(r"\b([A-Z]{4})\b")
_ICAO_VALID_PREFIXES = frozenset("KLBCEPRVWYZ")
_ICAO_VALID_DIPREFIXES = frozenset([
    "EG", "LF", "RJ", "RK", "WS", "VH", "ZS", "SA", "SB", "CY", "OM",
])


def _icao_valid_prefix(code: str) -> bool:
    return code[0] in _ICAO_VALID_PREFIXES or code[:2] in _ICAO_VALID_DIPREFIXES


def _extract_icao_from_text(
    full_text: str,
    source_text: Optional[str] = None,
    rules_text: Optional[str] = None,
) -> Optional[str]:
    """
    Extract a 4-letter ICAO station code with three zones of trust:

    Zone 1 — Trusted fields (resolution_source + rules): any valid-prefix code
              not on the denylist is accepted without further context.
    Zone 2 — Full combined text with station-context requirement: accepted only
              when a station keyword ("station", "airport", "METAR", etc.) appears
              within 100 characters of the candidate.
    Zone 3 — Known-station whitelist: accepted from anywhere in combined text if
              the code is in _KNOWN_ICAO_CODES (loaded from stations.yaml).
    """
    def _accepted(code: str) -> bool:
        return code not in _ICAO_DENYLIST and _icao_valid_prefix(code)

    # Zone 1: trusted source / rules fields
    for zone in (source_text, rules_text):
        if not zone:
            continue
        for m in _ICAO_PATTERN.finditer(zone.upper()):
            if _accepted(m.group(1)):
                return m.group(1)

    # Zone 2: full combined text but only near station-context keywords
    text_upper = full_text.upper()
    text_lower = full_text.lower()
    for m in _ICAO_PATTERN.finditer(text_upper):
        code = m.group(1)
        if not _accepted(code):
            continue
        start = max(0, m.start() - 100)
        end = min(len(text_lower), m.end() + 100)
        window = text_lower[start:end]
        if any(kw in window for kw in _STATION_CONTEXT_KWS):
            return code

    # Zone 3: known-station whitelist — accepted from anywhere
    for m in _ICAO_PATTERN.finditer(full_text.upper()):
        if m.group(1) in _KNOWN_ICAO_CODES:
            return m.group(1)

    return None


def _extract_resolution_source(text: str) -> Optional[str]:
    patterns = [
        re.compile(
            r"(?:according to|based on|source:|settlement:|resolved by|data from|using data from)\s+(.+?)(?:\.|$|\|)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:wunderground|weather underground|noaa|nws|metar|weather\.gov|aviationweather|hko|met office)",
            re.IGNORECASE,
        ),
    ]
    for p in patterns:
        m = p.search(text)
        if m:
            try:
                return m.group(1).strip()
            except IndexError:
                return m.group(0).strip()
    return None


def _extract_station_url(text: str) -> Optional[str]:
    m = re.search(r"https?://\S+", text)
    if m:
        return m.group(0)
    return None


def _find_risk_keywords(text: str) -> list[str]:
    q = text.lower()
    found = []
    for kw in RISK_KEYWORDS:
        if kw.lower() in q:
            found.append(kw)
    return found


def parse_market(
    market_id: str,
    question: str,
    outcomes: Optional[list[str]] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    rules: Optional[str] = None,
    resolution_text: Optional[str] = None,
    close_time: Optional[str] = None,
) -> ParsedMarket:
    """
    Parse a market into structured fields. Never raises.
    Combines question + title + description + rules for richer parsing.

    close_time is used ONLY to infer the year for partial dates ("April 26").
    It is never used as the target date itself.
    """
    try:
        return _parse_inner(
            market_id, question, outcomes, title, description,
            rules, resolution_text, close_time,
        )
    except Exception as exc:
        logger.exception("Unexpected parse error for market %s: %s", market_id, exc)
        return ParsedMarket(
            market_id=market_id,
            question=question,
            parse_failed=True,
            parse_failure_reason=f"unexpected_exception: {exc}",
        )


def parse_market_from_raw(market_id: str, raw: dict) -> ParsedMarket:
    """
    Parse directly from raw Gamma/CLOB market JSON dict.
    Extracts all available text fields automatically.
    """
    question = str(raw.get("question") or "")
    title = raw.get("title") or raw.get("groupItemTitle") or raw.get("name")
    description = raw.get("description") or raw.get("longDescription")
    rules = raw.get("rules") or raw.get("resolutionRules") or raw.get("resolution_rules")
    resolution_text = raw.get("resolutionSource") or raw.get("resolution_source")

    outcomes_raw = raw.get("outcomes") or []
    if isinstance(outcomes_raw, str):
        import json as _json
        try:
            outcomes_raw = _json.loads(outcomes_raw)
        except Exception:
            outcomes_raw = []
    outcomes = [str(o) for o in outcomes_raw]

    close_time = (
        raw.get("endDate") or raw.get("closeTime")
        or raw.get("end_date_iso") or raw.get("close_time")
    )
    return parse_market(
        market_id=market_id,
        question=question,
        outcomes=outcomes,
        title=str(title) if title else None,
        description=str(description) if description else None,
        rules=str(rules) if rules else None,
        resolution_text=str(resolution_text) if resolution_text else None,
        close_time=str(close_time) if close_time else None,
    )


def _parse_inner(
    market_id: str,
    question: str,
    outcomes: Optional[list[str]],
    title: Optional[str],
    description: Optional[str],
    rules: Optional[str],
    resolution_text: Optional[str],
    close_time: Optional[str] = None,
) -> ParsedMarket:
    combined, confidence = _combine_text(question, title, description, rules, resolution_text)

    market_type = _detect_market_type(combined)
    is_precipitation = market_type == MARKET_TYPE_PRECIPITATION

    unit = _detect_unit(combined)
    city = _extract_city(_normalize(question))  # city extraction from question primarily
    reference_date = _parse_reference_date(close_time)
    date_str, parsed_target_date = _extract_date(combined, reference_date=reference_date)

    bucket_info = _extract_bucket(combined, unit)
    bucket_low = bucket_info["bucket_low"]
    bucket_high = bucket_info["bucket_high"]
    open_ended_low = bucket_info["open_ended_low"]
    open_ended_high = bucket_info["open_ended_high"]
    bucket_label = bucket_info["bucket_label"]

    # Fallback: try outcomes list
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

    resolution_source = _extract_resolution_source(combined)
    station_url = _extract_station_url(combined)
    source_type = _classify_source_type(combined)
    station_code_from_text = _extract_icao_from_text(
        combined,
        source_text=resolution_text,
        rules_text=rules,
    )
    risk_kw = _find_risk_keywords(combined)

    manipulation_flag = False
    combined_lower = combined.lower()
    if city and city.lower() in MANIPULATION_CITIES:
        manipulation_flag = True
    for kw in ["paris", "cdg", "le bourget", "le_bourget"]:
        if kw in combined_lower:
            manipulation_flag = True

    # Parse failure assessment
    parse_failed = False
    parse_failure_parts = []

    if market_type == MARKET_TYPE_UNKNOWN and not is_precipitation:
        if "temperature" not in combined_lower and "°" not in combined and "degrees" not in combined_lower:
            parse_failed = True
            parse_failure_parts.append("market_type_undetermined")

    if bucket_label is None and not is_precipitation:
        parse_failed = True
        parse_failure_parts.append("bucket_not_parseable")

    parse_failure_reason = "|".join(parse_failure_parts) or None

    # Forecast blocking: missing target date blocks PAPER_ actions
    forecast_blocked_reason = None
    if parsed_target_date is None and not is_precipitation:
        forecast_blocked_reason = "missing_target_date"

    exact_boundary = "unknown"
    if "inclusive" in combined_lower or "or equal" in combined_lower:
        exact_boundary = "inclusive"
    elif "exclusive" in combined_lower or "strictly" in combined_lower:
        exact_boundary = "exclusive"

    return ParsedMarket(
        market_id=market_id,
        question=_normalize(question),
        parsed_source_text=combined,
        parsed_source_confidence=confidence,
        city=city,
        date_str=date_str,
        parsed_target_date=parsed_target_date,
        market_type=market_type,
        unit=unit,
        bucket_label=bucket_label,
        bucket_low=bucket_low,
        bucket_high=bucket_high,
        open_ended_low=open_ended_low,
        open_ended_high=open_ended_high,
        exact_boundary=exact_boundary,
        resolution_source_text=resolution_source,
        source_type=source_type,
        station_code_from_text=station_code_from_text,
        station_url=station_url,
        risk_keywords_found=risk_kw,
        manipulation_flag=manipulation_flag,
        parse_failed=parse_failed,
        parse_failure_reason=parse_failure_reason,
        is_precipitation=is_precipitation,
        forecast_blocked_reason=forecast_blocked_reason,
    )


def parse_markets_bulk(markets: list[dict]) -> list[ParsedMarket]:
    results = []
    for m in markets:
        mid = str(m.get("market_id") or m.get("id") or "")
        q = str(m.get("question") or "")
        outcomes = m.get("outcomes") or []
        if not mid or not q:
            continue
        results.append(parse_market(mid, q, outcomes))
    return results
