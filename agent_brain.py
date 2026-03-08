#!/usr/bin/env python3
"""
AgentBrain — Claude-Powered Trading Decision Engine (Mart 2026)
===============================================================
Milyar: Polymarket trading için kural tabanlı _analyze() mantığını
Claude claude-sonnet-4-6 tabanlı agent kararına dönüştürür.

Mimari:
  Veri Toplama (Python) → JSON Bağlam → Claude API → Yapılandırılmış Karar → Yürütme (Python)

Claude araçları (READ-ONLY):
  - get_ofi_signal       : OFI buffer durumu (BTC/USDT 15dk kümülatif)
  - get_market_state     : Piyasa detayları (fiyat, likidite, süre)
  - check_open_positions : Açık pozisyonlar
  - get_trade_history    : Son N işlem geçmişi

Karar aracı (zorunlu sonuç):
  - make_trading_decision: BUY_YES | BUY_NO | WAIT + gerekçe + güven

Çok turlu bellek:
  Session boyunca conversation_history tutulur.
  Claude geçmiş kararları ve sonuçları görebilir.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Callable, List

import anthropic

# ─────────────────────────────────────────── Model ve Sabitler

MODEL = "claude-sonnet-4-6"
MAX_TOOL_ROUNDS = 4       # Claude max kaç araç turu kullanabilir
MAX_HISTORY_MSGS = 30     # Bellek sıkıştırma eşiği (mesaj sayısı)

SYSTEM_PROMPT = """\
Sen Polymarket prediction market trading botu için bir yapay zeka karar motorusun.
Görevin: Verilen piyasa bağlamını analiz et ve BUY_YES, BUY_NO ya da WAIT kararını ver.

═══ STRATEJİ (V16 Agent Edition) ═══

PIYASA SEÇİMİ:
• 1 saat – 7 gün süreli piyasalar (5dk/15dk BTC piyasaları YASAK — negatif EV kanıtlandı)
• Fiyat bandı: YES için 0.30–0.45, NO için 0.55–0.70 (YES fiyatı açısından)
• Minimum $1000 likidite

OFI SİGNAL KURALI:
• OFI ratio ≥ 3.0x VE Z-skor ≥ ±2.0 gerekli
• Alıcı baskısı → YES sinyali → YES al (fiyat 0.30–0.45 bandında)
• Satıcı baskısı → NO sinyali  → NO al  (YES fiyatı 0.55–0.70 bandında)

KELLY KRİTERİ:
• f* = (b×p − q) / b  →  Half-Kelly (×0.5) uygula
• Min stake: $2.50, Max: bankroll × %50

ÇIKIŞ STRATEJİSİ:
• 1H+ binary piyasalar: Sadece settlement bekle (erken çıkış YASAK)
• Erken çıkış spread ve likidite kaybı yaratır

═══ BİLİNEN RİSKLER (Geçmiş Analizden) ═══

1. ORACLE GECİKMESİ (~18 sn):
   Chainlink oracle'ı piyasa fiyatından 18 saniye geri kalabilir.
   Piyasa çöküşlerinde erken çıkış şansı kaybedilir.
   → Eğer ticaret sinyali ani fiyat hareketi sonrasıysa risk YÜKSEK.

2. FEE DRAG ($0.11/işlem):
   $30 bankroll'da her işlem ~%15 kar marjı eriyor.
   EV $0.11'in altındaysa WAIT daha akıllıca.

3. ASİMETRİK KAYIP:
   %70.8 win rate'e rağmen net -$11.74 kayıp (24 işlem).
   Büyük kayıplar (−$4.51 tek işlem) 5-6 kazancı siliyor.
   → Düşük EV veya zayıf sinyalde TRADE ETME.

═══ KARAR VERİRKEN ═══
• Araçları kullanarak ek bilgi toplayabilirsin
• Son adımda MUTLAKA make_trading_decision aracını çağır
• Gerekçeni kısa ve net yaz (max 100 karakter)
• Şüphe varsa WAIT — yanlış işlem yapmamak, doğru işlem yapmaktan değerlidir
"""

# ─────────────────────────────────────────── Araç Tanımları

# READ-ONLY araçlar + zorunlu karar aracı
TOOLS = [
    {
        "name": "get_ofi_signal",
        "description": (
            "Şu anki Order Flow Imbalance (OFI) sinyalini al. "
            "BTC/USDT'de 15dk kümülatif alıcı/satıcı baskısını ölçer. "
            "Ratio, z-skor ve sinyal yönünü döndürür."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_market_state",
        "description": (
            "Belirli bir piyasanın anlık detaylarını al: "
            "YES/NO fiyatları, likidite, kalan süre, fee durumu."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "market_id": {
                    "type": "string",
                    "description": "Piyasa ID'si",
                }
            },
            "required": ["market_id"],
        },
    },
    {
        "name": "check_open_positions",
        "description": "Şu anda açık olan tüm pozisyonları listele.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_trade_history",
        "description": "Son N işlemin geçmişini al (sonuç, PnL, kullanılan sinyal).",
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "Kaç işlem geçmişi isteniyor (varsayılan: 10)",
                }
            },
            "required": [],
        },
    },
    {
        "name": "make_trading_decision",
        "description": (
            "ZORUNLU SON ADIM: Ticaret kararını kaydet. "
            "Analiz tamamlandıktan sonra mutlaka bu aracı çağır."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["BUY_YES", "BUY_NO", "WAIT"],
                    "description": "Yapılacak işlem",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Kararın kısa gerekçesi (max 120 karakter)",
                },
                "confidence": {
                    "type": "number",
                    "description": "Karar güveni 0.0–1.0 arası",
                },
                "stake_override_usd": {
                    "type": ["number", "null"],
                    "description": (
                        "Kelly'den farklı stake belirlemek istiyorsan USD cinsinden. "
                        "null = Kelly hesabını kullan"
                    ),
                },
                "flags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Uyarı bayrakları (örn: ['ORACLE_LAG', 'HIGH_FEE'])",
                },
            },
            "required": ["action", "reasoning", "confidence"],
            "additionalProperties": False,
        },
    },
]


# ─────────────────────────────────────────── TradingDecision

class TradingDecision:
    """Claude'un make_trading_decision çağrısından üretilen karar nesnesi."""

    def __init__(
        self,
        action: str,
        reasoning: str,
        confidence: float,
        stake_override: Optional[float] = None,
        flags: Optional[List[str]] = None,
    ):
        self.action = action              # BUY_YES | BUY_NO | WAIT
        self.reasoning = reasoning[:120]  # kısa tut
        self.confidence = float(confidence)
        self.stake_override = stake_override  # None → Kelly kullan
        self.flags = flags or []

    @classmethod
    def wait(cls, reason: str) -> "TradingDecision":
        return cls("WAIT", reason, 0.0)

    @property
    def is_buy(self) -> bool:
        return self.action in ("BUY_YES", "BUY_NO")

    def __repr__(self) -> str:
        stake_info = f" stake=${self.stake_override:.2f}" if self.stake_override else ""
        flags_info = f" [{','.join(self.flags)}]" if self.flags else ""
        return (
            f"TradingDecision({self.action} "
            f"conf={self.confidence:.0%}"
            f"{stake_info}{flags_info} "
            f"'{self.reasoning}')"
        )


# ─────────────────────────────────────────── AgentBrain

class AgentBrain:
    """
    Claude claude-sonnet-4-6 tabanlı ticaret karar motoru.

    Kullanım (btc_sniper_v16.py içinden):
        brain = AgentBrain(api_key="sk-ant-...")
        brain.register_tool("get_ofi_signal", lambda: {...})
        brain.register_tool("get_market_state", lambda market_id: {...})
        brain.register_tool("check_open_positions", lambda: [...])
        brain.register_tool("get_trade_history", lambda n=10: [...])

        decision = await brain.decide(market_id, market_context_dict)
        if decision.action == "BUY_YES":
            ...
    """

    def __init__(self, api_key: Optional[str] = None):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise ValueError(
                "ANTHROPIC_API_KEY bulunamadı! "
                "config_v16.json'a 'anthropic_api_key' ekle veya "
                "ANTHROPIC_API_KEY ortam değişkeni ayarla."
            )
        self.client = anthropic.Anthropic(api_key=key)

        # Session boyunca çok turlu bellek
        self.conversation_history: List[Dict] = []

        # Python callback'leri (bot tarafından kaydedilir)
        self._tool_callbacks: Dict[str, Callable] = {}

        # İstatistik
        self.total_decisions = 0
        self.buy_decisions   = 0
        self.wait_decisions  = 0
        self.api_errors      = 0

    # ──────────── Tool Kayıt

    def register_tool(self, name: str, callback: Callable) -> None:
        """Araç callback'ini kaydet (bot __init__ içinde yapılır)."""
        self._tool_callbacks[name] = callback

    # ──────────── Ana Karar Metodu

    async def decide(
        self,
        market_id: str,
        market_context: Dict[str, Any],
    ) -> TradingDecision:
        """
        Claude'a piyasa bağlamını gönder, araç turu yap, karar al.

        market_context anahtarları:
          market_question, yes_price, hours_left, liquidity_usd,
          fees_enabled, ofi_ratio, ofi_z, ofi_signal, kelly_stake,
          ev_usd, entry_price, bankroll, session_pnl, daily_pnl,
          open_positions, wins, losses
        """
        user_msg = self._build_user_message(market_id, market_context)
        self.conversation_history.append({"role": "user", "content": user_msg})

        try:
            decision = await asyncio.to_thread(self._run_agent_loop)
        except Exception as exc:
            self.api_errors += 1
            decision = TradingDecision.wait(f"Agent hatası: {str(exc)[:60]}")

        self.total_decisions += 1
        if decision.is_buy:
            self.buy_decisions += 1
        else:
            self.wait_decisions += 1

        return decision

    def notify_trade_result(
        self,
        market_question: str,
        action: str,
        pnl_usd: float,
        result_type: str,
    ) -> None:
        """
        Gerçekleşen işlem sonucunu Claude'a bildir (bellek güncellemesi).
        _settle() içinden çağrılmalı.
        """
        msg = (
            f"[SONUÇ] {action} → {result_type} "
            f"PnL: ${pnl_usd:+.3f} | "
            f"Piyasa: {market_question[:60]}"
        )
        self.conversation_history.append({"role": "user", "content": msg})
        # Gerçek sonucu da assistant olarak ekle (Claude'un okuyabilmesi için)
        ack = f"Sonuç kaydedildi. PnL: ${pnl_usd:+.3f}"
        self.conversation_history.append({"role": "assistant", "content": ack})

    # ──────────── İç Metotlar

    def _build_user_message(self, market_id: str, ctx: Dict) -> str:
        ofi_sig  = ctx.get("ofi_signal") or "YOK (zayıf)"
        yes_p    = ctx.get("yes_price", 0.0)
        wr_label = ""
        wins     = ctx.get("wins", 0)
        losses   = ctx.get("losses", 0)
        total    = wins + losses
        if total > 0:
            wr_label = f" ({wins}W/{losses}L = {wins/total:.0%})"

        return (
            f"## Piyasa Analiz İsteği — {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC\n\n"
            f"**Soru:** {ctx.get('market_question', market_id)}\n"
            f"**Market ID:** `{market_id[:16]}...`\n\n"
            f"### Piyasa Durumu\n"
            f"| Alan | Değer |\n|---|---|\n"
            f"| YES fiyatı | {yes_p:.3f} |\n"
            f"| Kalan süre | {ctx.get('hours_left', 0):.1f} saat |\n"
            f"| Likidite | ${ctx.get('liquidity_usd', 0):,.0f} |\n"
            f"| Ücret | {'Evet' if ctx.get('fees_enabled') else '**ÜCRETSIZ**'} |\n\n"
            f"### OFI Sinyali (BTC/USDT 15dk kümülatif)\n"
            f"| Alan | Değer | Eşik |\n|---|---|---|\n"
            f"| Sinyal | **{ofi_sig}** | — |\n"
            f"| Alıcı/Satıcı oranı | {ctx.get('ofi_ratio', 0):.2f}x | ≥ 3.0x |\n"
            f"| Z-skor | {ctx.get('ofi_z', 0):.2f} | ≥ ±2.0 |\n\n"
            f"### Hesaplanan Değerler\n"
            f"| Alan | Değer |\n|---|---|\n"
            f"| Kelly stake | ${ctx.get('kelly_stake', 0):.2f} |\n"
            f"| Beklenen değer (EV) | ${ctx.get('ev_usd', 0):.3f} |\n"
            f"| Giriş fiyatı | {ctx.get('entry_price', 0):.3f} |\n\n"
            f"### Hesap Durumu\n"
            f"| Alan | Değer |\n|---|---|\n"
            f"| Bankroll | **${ctx.get('bankroll', 0):.2f}** |\n"
            f"| Seans PnL | ${ctx.get('session_pnl', 0):+.2f} |\n"
            f"| Günlük PnL | ${ctx.get('daily_pnl', 0):+.2f} |\n"
            f"| Açık pozisyon | {ctx.get('open_positions', 0)} |\n"
            f"| Seans performans | {wins + losses} işlem{wr_label} |\n\n"
            f"**Araçları kullanarak ek bilgi topla, sonra `make_trading_decision` çağır.**"
        )

    def _run_agent_loop(self) -> TradingDecision:
        """
        Senkron Claude API agentic loop.
        asyncio.to_thread() içinde çalışır.

        Akış:
          1. Claude mesaj gönder (tool_choice=auto)
          2. Claude araç çağırıyorsa → çalıştır, sonucu ekle → tekrar
          3. Claude make_trading_decision çağırıyorsa → kararı al, döndür
          4. MAX_TOOL_ROUNDS aşıldıysa → WAIT döndür
        """
        for round_idx in range(MAX_TOOL_ROUNDS + 1):
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=1024,
                thinking={"type": "adaptive"},
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                tool_choice={"type": "auto"},
                messages=self.conversation_history,
            )

            # make_trading_decision çağrısını ara
            decision_block = next(
                (b for b in response.content
                 if b.type == "tool_use" and b.name == "make_trading_decision"),
                None,
            )

            if decision_block is not None:
                # Kararı conversation'a ekle (sonuç bildirimi için)
                self.conversation_history.append(
                    {"role": "assistant", "content": response.content}
                )
                # Tool result olarak "kabul" döndür
                self.conversation_history.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": decision_block.id,
                        "content": "Karar kaydedildi.",
                    }],
                })
                return self._parse_decision_block(decision_block.input)

            # Sadece bilgi araçları mı çağrıldı?
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            if not tool_uses:
                # Hiç araç yok → conversation'a ekle, son tur
                self.conversation_history.append(
                    {"role": "assistant", "content": response.content}
                )
                # Kararı yok → WAIT
                return TradingDecision.wait("Agent make_trading_decision çağırmadı")

            # Araçları çalıştır, sonuçları topla
            self.conversation_history.append(
                {"role": "assistant", "content": response.content}
            )
            tool_results = []
            for tu in tool_uses:
                if tu.name == "make_trading_decision":
                    continue  # Yukarıda zaten yakalandı
                result_str = self._call_tool(tu.name, tu.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result_str,
                })
            if tool_results:
                self.conversation_history.append(
                    {"role": "user", "content": tool_results}
                )

        return TradingDecision.wait(f"Max tur aşıldı ({MAX_TOOL_ROUNDS})")

    def _call_tool(self, name: str, tool_input: Dict) -> str:
        """Kayıtlı Python callback'i çağır, JSON döndür."""
        cb = self._tool_callbacks.get(name)
        if cb is None:
            return json.dumps({"hata": f"'{name}' aracı kayıtlı değil"})
        try:
            result = cb(**tool_input)
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as exc:
            return json.dumps({"hata": str(exc)})

    def _parse_decision_block(self, inp: Dict) -> TradingDecision:
        """make_trading_decision araç girişini TradingDecision'a dönüştür."""
        action = inp.get("action", "WAIT")
        if action not in ("BUY_YES", "BUY_NO", "WAIT"):
            action = "WAIT"
        return TradingDecision(
            action=action,
            reasoning=str(inp.get("reasoning", ""))[:120],
            confidence=float(inp.get("confidence", 0.5)),
            stake_override=inp.get("stake_override_usd"),
            flags=inp.get("flags", []),
        )

    # ──────────── Bellek Yönetimi

    def compact_history(self) -> int:
        """
        Conversation geçmişi çok uzadıysa sıkıştır.
        Son MAX_HISTORY_MSGS//2 mesajı koru, geri kalanları özetle.
        Döndürür: silinen mesaj sayısı.
        """
        if len(self.conversation_history) <= MAX_HISTORY_MSGS:
            return 0

        keep_last = MAX_HISTORY_MSGS // 2
        dropped = len(self.conversation_history) - keep_last
        summary_msg = (
            f"[Bellek sıkıştırıldı: {dropped} eski mesaj özetlendi. "
            f"Toplam karar: {self.total_decisions}, "
            f"Alım: {self.buy_decisions}, WAIT: {self.wait_decisions}]"
        )
        self.conversation_history = (
            [{"role": "user", "content": summary_msg}]
            + self.conversation_history[-keep_last:]
        )
        return dropped

    # ──────────── İstatistik

    @property
    def stats(self) -> Dict:
        return {
            "toplam_karar":   self.total_decisions,
            "alim_kararı":    self.buy_decisions,
            "wait_kararı":    self.wait_decisions,
            "api_hatası":     self.api_errors,
            "bellek_mesajı":  len(self.conversation_history),
            "model":          MODEL,
        }
