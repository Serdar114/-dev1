"""
core/market_discovery.py — V21 Polymarket aktif BTC 5m market keşfi.

Doğrulanmış slug formatı: btc-updown-5m-{timestamp}
Gamma API üzerinden aktif marketler listelenir, 5m BTC filtresi uygulanır.
Geçerli zaman penceresi: 30s – 350s kalan süre.
"""

import asyncio
import json
import time
import utils.logger as logger_mod
from datetime import datetime, timezone

log = logger_mod.get("market_discovery")


class MarketDiscovery:
    def __init__(self, cfg: dict, state):
        self._cfg = cfg
        self._state = state
        net = cfg.get("network", {})
        self._gamma_url = net.get("gamma_url", "https://gamma-api.polymarket.com")
        self._clob_url = net.get("clob_url", "https://clob.polymarket.com")

    # ── Ana keşif metodu ──────────────────────────────────────────────────────

    async def find_active_market(self) -> bool:
        """
        Aktif BTC 5m market arar.
        Bulursa state.market'e yazar ve True döner.
        Bulamazsa False döner.
        """
        try:
            import aiohttp
        except ImportError:
            raise RuntimeError("'aiohttp' paketi eksik: pip install aiohttp")

        try:
            markets = await self._fetch_gamma_markets()
            candidate = self._pick_best(markets)
            if candidate:
                m, end_time = candidate
                self._apply(m, end_time)
                return True
            log.debug("Aktif BTC 5m market bulunamadı")
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Market discovery hatası: %s", exc)
            return False

    # ── Gamma API sorgusu ────────────────────────────────────────────────────

    async def _fetch_gamma_markets(self) -> list:
        import aiohttp

        url = f"{self._gamma_url}/markets"
        params = {
            "active":    "true",
            "closed":    "false",
            "limit":     100,
            "order":     "volume",
            "ascending": "false",
        }
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, params=params) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Gamma API HTTP {resp.status}")
                data = await resp.json(content_type=None)

        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("markets", [])
        return []

    # ── Filtreleme ve seçim ──────────────────────────────────────────────────

    def _pick_best(self, markets: list):
        """BTC 5m market listesinden en uygununu seçer."""
        now = time.time()
        candidates = []

        for m in markets:
            slug = (m.get("slug") or m.get("market_slug") or "").lower()
            question = (m.get("question") or "").lower()

            # BTC filtresi
            is_btc = "btc" in slug or "bitcoin" in question
            if not is_btc:
                continue

            # 5m filtresi
            is_5m = (
                "5m" in slug
                or "5-min" in slug
                or "5 minute" in question
                or "5min" in question
            )
            if not is_5m:
                continue

            # 15m devre dışı
            if self._cfg.get("mode", {}).get("enable_15m", False) is False:
                if "15m" in slug or "15 minute" in question:
                    continue

            end_time = self._parse_end_time(m)
            if not end_time:
                continue

            secs_left = end_time - now
            if secs_left < 30 or secs_left > 350:
                continue  # çok yakın veya çok uzak

            candidates.append((m, end_time, secs_left))

        if not candidates:
            return None

        # En az kalan süreli ama geçerli olanı al (giriş penceresine yakın)
        candidates.sort(key=lambda x: x[2])
        best = candidates[0]
        return best[0], best[1]

    def _parse_end_time(self, m: dict) -> float:
        for key in ("endDate", "end_date", "end_time", "endTime", "end"):
            v = m.get(key)
            if not v:
                continue
            try:
                if isinstance(v, (int, float)):
                    ts = float(v)
                    # Unix ms → s dönüşümü
                    if ts > 1e10:
                        ts /= 1000.0
                    return ts
                v_str = str(v).strip()
                if "Z" in v_str:
                    v_str = v_str.replace("Z", "+00:00")
                dt = datetime.fromisoformat(v_str)
                return dt.timestamp()
            except Exception:
                continue
        return 0.0

    def _extract_token_ids(self, m: dict) -> tuple:
        """(up_token_id, down_token_id) döner. Bulamazsa ("", "")."""
        raw = m.get("clobTokenIds") or m.get("clob_token_ids") or ""

        if isinstance(raw, list):
            ids = [str(t) for t in raw]
        elif isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                ids = [str(t) for t in parsed] if isinstance(parsed, list) else []
            except Exception:
                ids = []
        else:
            ids = []

        if len(ids) < 2:
            # tokens field'ı dene
            tokens = m.get("tokens", [])
            if isinstance(tokens, list):
                ids = [
                    str(t.get("token_id", t)) if isinstance(t, dict) else str(t)
                    for t in tokens
                ]

        if len(ids) >= 2:
            return ids[0], ids[1]
        return "", ""

    def _apply(self, m: dict, end_time: float) -> None:
        info = self._state.market
        info.slug = m.get("slug") or m.get("market_slug") or ""
        info.condition_id = m.get("conditionId") or m.get("condition_id") or ""
        info.question = m.get("question") or ""
        info.end_time = end_time
        info.market_start = time.time()

        up_id, down_id = self._extract_token_ids(m)
        info.up_token_id = up_id
        info.down_token_id = down_id

        secs = end_time - time.time()
        log.info(
            "Market bulundu: %s | kalan=%.0fs | up=%s... down=%s...",
            info.slug, secs,
            up_id[:12] if up_id else "?",
            down_id[:12] if down_id else "?",
        )
        self._state.log_event(f"Market: {info.slug[:45]} ({secs:.0f}s)")
