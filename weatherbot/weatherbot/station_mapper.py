"""
Map parsed city names to ICAO station codes and coordinates.

Loads from config/stations.yaml.
Unknown cities are allowed in paper mode — logged as station_unknown.
"""
import logging
import os
from dataclasses import dataclass
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "stations.yaml")


@dataclass
class StationInfo:
    city_key: str
    icao: str
    lat: float
    lon: float
    unit: str  # "F" or "C"
    timezone: str
    risk: str  # "low" / "medium" / "high"
    blacklist: bool = False
    notes: Optional[str] = None


_stations: dict[str, StationInfo] = {}
_aliases: dict[str, str] = {}
_loaded = False


def _load():
    global _stations, _aliases, _loaded
    if _loaded:
        return

    config_path = os.path.abspath(_CONFIG_PATH)
    if not os.path.exists(config_path):
        logger.warning("stations.yaml not found at %s", config_path)
        _loaded = True
        return

    with open(config_path, "r") as f:
        data = yaml.safe_load(f)

    raw_stations = data.get("stations") or {}
    raw_aliases = data.get("aliases") or {}

    for city_key, info in raw_stations.items():
        if not isinstance(info, dict):
            continue
        try:
            si = StationInfo(
                city_key=city_key,
                icao=info["icao"],
                lat=float(info["lat"]),
                lon=float(info["lon"]),
                unit=info.get("unit", "F").upper(),
                timezone=info.get("timezone", "UTC"),
                risk=info.get("risk", "low"),
                blacklist=bool(info.get("blacklist", False)),
                notes=info.get("notes"),
            )
            _stations[city_key.lower()] = si
        except (KeyError, ValueError) as exc:
            logger.warning("Bad station entry for %s: %s", city_key, exc)

    for alias, canonical in raw_aliases.items():
        _aliases[alias.lower()] = canonical.lower()

    logger.info("Loaded %d stations and %d aliases", len(_stations), len(_aliases))
    _loaded = True


def _normalize(name: str) -> str:
    return name.lower().strip()


def lookup(city_name: str) -> Optional[StationInfo]:
    """
    Return StationInfo for the given city name, or None if not found.
    Handles aliases and case-insensitive matching.
    """
    _load()
    key = _normalize(city_name)

    # Direct match
    if key in _stations:
        return _stations[key]

    # Alias match
    canonical = _aliases.get(key)
    if canonical and canonical in _stations:
        return _stations[canonical]

    # Fuzzy: check if any known city name is contained in the query
    for city_key, info in _stations.items():
        if city_key in key or key in city_key:
            return info

    return None


def is_blacklisted(city_name: Optional[str]) -> bool:
    """Return True if city is on the hard-reject list."""
    if not city_name:
        return False
    _load()
    key = _normalize(city_name)
    # Check direct manipulation keywords
    for bad in ("paris", "cdg", "le bourget", "le_bourget"):
        if bad in key:
            return True
    # Check station entry
    si = lookup(city_name)
    if si and si.blacklist:
        return True
    return False


def get_city_unit(city_name: Optional[str]) -> Optional[str]:
    """Return expected unit (F/C) for a city if known."""
    if not city_name:
        return None
    si = lookup(city_name)
    if si:
        return si.unit
    return None


def get_station_risk(city_name: Optional[str]) -> str:
    """Return risk level string: low / medium / high / unknown."""
    if not city_name:
        return "unknown"
    si = lookup(city_name)
    if si:
        return si.risk
    return "unknown"


def all_known_cities() -> list[str]:
    _load()
    return sorted(_stations.keys())


def map_parsed_market(city: Optional[str]) -> dict:
    """
    Return a dict with station mapping info for use in pipeline.

    Returns:
        {
            "icao": str or None,
            "lat": float or None,
            "lon": float or None,
            "unit": str or None,
            "timezone": str or None,
            "risk": str,
            "blacklist": bool,
            "station_known": bool,
            "notes": str or None,
        }
    """
    _load()
    si = lookup(city) if city else None

    if si:
        return {
            "icao": si.icao,
            "lat": si.lat,
            "lon": si.lon,
            "unit": si.unit,
            "timezone": si.timezone,
            "risk": si.risk,
            "blacklist": si.blacklist,
            "station_known": True,
            "notes": si.notes,
        }

    # Unknown city — allowed in paper mode
    blacklisted = is_blacklisted(city)
    return {
        "icao": None,
        "lat": None,
        "lon": None,
        "unit": None,
        "timezone": None,
        "risk": "high" if blacklisted else "unknown",
        "blacklist": blacklisted,
        "station_known": False,
        "notes": "station_unknown",
    }
