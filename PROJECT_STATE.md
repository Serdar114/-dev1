# PROJECT STATE

## Repo root
`polybot/` dizini (branch: `claude/polymarket-core-modules-RHYZk`).
Ana repo (`serdar114/-dev1`) birden fazla branch içeriyor; her biri farklı bir iterasyon:
- `claude/polymarket-core-modules-RHYZk` → `polybot/` — **en güncel ve CLAUDE.md ile en uyumlu versiyon**
- `claude/polymarket-btc-harness-0fJwg` → flat root (fee_math.py, taker_shadow.py, maker_shadow.py, vb.) — daha eski harness denemesi
- `claude/phase-1-trading-system-e7Gvn` → `polybot_v2/` — v2 yeniden yazım denemesi (src/ + tests/)
- `claude/dual-feed-adapter-support-1RVz5` → modüler yapı (config/, discovery/, execution/, feeds/, vb.)
- `claude/build-observation-bot-4XDJP` → `polybot_obs/` — observation bot denemesi
- `main` → sadece README.md + index.html (boş)

Bu dosya `polybot/` dizinini repo root olarak kabul eder.

## Repo map
```
polybot/
├── binance_feed.py       # Binance WS bookTicker client (BTC mid price)
├── config.json           # Tüm parametreler (bankroll, fee, entry window, vb.)
├── fee_engine.py         # Fee hesaplama (yeni formül: C × p × feeRate × (p×(1-p))^exp)
├── logger.py             # JSONL logger (günlük dosya, async + sync)
├── main.py               # Entry point — PolyBot sınıfı, async event loop
├── market_discovery.py   # Gamma API + CLOB fallback ile market keşfi
├── paper_trader.py       # Dual Side Capture paper trading simülasyonu
├── polymarket_feed.py    # Polymarket CLOB WS feed (orderbook stream + HTTP fallback)
├── risk_manager.py       # Kill conditions, bankroll floor, günlük trade limiti
├── signal_engine.py      # Dual Side Capture sinyal motoru (pair_sum bazlı)
└── logs/                 # Runtime JSONL logları (gitignore'da olmalı)
```

## Main modules and what they do

### main.py
Entry point. PolyBot sınıfı: Binance WS + Polymarket WS başlatır, her 5m pencerede market discovery yapar, signal_engine'den sinyal alır, paper_trader ile pozisyon açar, pencere kapanınca resolve eder. `--once` modu ve `--config` parametresi destekler.

### market_discovery.py
Aktif BTC Up/Down marketini 3 kademeli fallback ile bulur: (1) slug ile Gamma API, (2) tag/keyword filtresi, (3) CLOB /markets endpoint. clobTokenIds JSON string parse bug fix'i içerir. Slug formatı: `btc-updown-5m-{unix_ts}`. Pencere kapanmasına <60s kalmışsa skip eder.

### signal_engine.py
Dual Side Capture stratejisi. BTC yönü/deltası KULLANILMIYOR. Mantık: `pair_sum = up_ask + down_ask`, `net_edge = 1.0 - pair_sum`. pair_sum < target_sum_max (0.95) ise DUAL_ENTRY sinyali verir. Koşullar: entry window, orderbook mevcudiyeti, spread kontrolü, depth kontrolü, pair_sum eşiği.

### paper_trader.py
Her iki tarafı (UP + DOWN) ask fiyatından fill edilmiş kabul eder. Pencere kapanınca Binance REST'ten btc_close çeker, kazanan tarafı belirler (logging için — PnL her durumda aynı: `shares × net_edge`). Fee = 0 varsayımı (post-only maker). Fill rate sabit %100.

### fee_engine.py
Yeni fee formülü: `fee = C × p × feeRate × (p × (1-p))^exponent`. Default feeRate=0.072, exponent=1. compute_fee(), compute_fee_pct(), net_pnl(), is_edge_positive() fonksiyonları var. **Ancak paper_trader fee_engine'i kullanmıyor — fee=0 varsayıyor.**

### risk_manager.py
Kill koşulları: (1) max_consecutive_losses (4), (2) bankroll < floor (%70), (3) günlük max_daily_trades (5). Pozisyon boyutlama sabit min_shares=5. Kelly sizing "ileride eklenebilir" notu var ama eklenmemiş.

### binance_feed.py
Binance WS bookTicker stream. Reconnect destekli. Pencere başında open_price capture eder (ilk N saniye). Subscriber callback pattern.

### polymarket_feed.py
Polymarket CLOB WS market stream. BookSnapshot dataclass (bid, ask, mid, spread, depth). WS subscribe + HTTP /book fallback polling. Incremental price_change event handling. Debug loglaması yoğun (PM_DEBUG print'leri).

### logger.py
Günlük JSONL dosyasına yazar (logs/polybot-YYYY-MM-DD.jsonl). Async ve sync versiyonları var.

### config.json
Tüm parametreler tek dosyada. bankroll=30, shares_per_side=5, fee_rate=0.072, target_sum_max=0.95, mode=paper, market_type=5m. API key alanları boş.

## Truth-critical file status

| Dosya | Durum | Not |
|---|---|---|
| fee_engine.py | **MEVCUT** | Formül var ama paper_trader tarafından kullanılmıyor. İzole durumda. |
| market_discovery.py | **MEVCUT** | 3-fallback keşif var. clobTokenIds bug fix uygulanmış. Token UP/DOWN index sırası varsayımsal (index 0=UP, 1=DOWN — doğrulanmamış). |
| resolution_truth.py | **YOK** | Chainlink resolution truth modülü hiç yazılmamış. Doctrine'da primary truth olarak belirtiliyor. |
| truth_logger.py | **YOK** | logger.py var ama truth_logger.py ayrı bir modül olarak mevcut değil. |
| analyzer.py | **YOK** | Trade analiz modülü hiç yazılmamış. |

## Execution-critical file status

| Dosya | Durum | Not |
|---|---|---|
| signal_engine.py | **MEVCUT** | Dual Side Capture mantığı çalışır görünüyor. Yön tahmini yok, pair_sum bazlı. |
| paper_trader.py | **MEVCUT** | Paper mode only. Fee=0 varsayımı. %100 fill rate. Gerçek order yok. |
| execution_router.py | **YOK** | Gerçek order execution modülü hiç yazılmamış. |
| risk_manager.py | **MEVCUT** | Kill conditions var. Kelly sizing yok. |
| main.py | **MEVCUT** | Event loop çalışır görünüyor. Windows ProactorEventLoop uyarısı var. |

## Current measurement-layer status
- **Ölçüm katmanı eksik.** resolution_truth.py, truth_logger.py, analyzer.py yok.
- Chainlink resolution truth hiç implemente edilmemiş. Binance REST btc_close ile "kazanan taraf" belirleniyor ama bu Polymarket'in gerçek resolution kaynağı (Chainlink) ile karşılaştırılmıyor.
- Trade sonuçları logger.py ile JSONL'e yazılıyor ama yapılandırılmış analiz (win rate, edge decay, fill toxicity, vs.) yapan bir modül yok.
- fee_engine.py mevcut ama paper_trader'a entegre değil — paper PnL hesabı fee'yi görmezden geliyor.

## Current strategy-layer status
- **Dual Side Capture**: UP ve DOWN tarafı aynı anda alınıyor. pair_sum < 0.95 ise giriş yapılıyor.
- Strateji BTC yönü/deltası kullanmıyor — pure pair_sum arbitrajı.
- Entry window: pencere başından 240s sonra başlıyor, kapanmadan 10s önce bitiyor.
- Spread ve depth filtreleri var (max_spread_pct=2.0%, min_depth=50 shares).
- Tek pencerede tek sinyal — ilk DUAL_ENTRY'den sonra pencere boyunca skip.
- Maker/post-only order varsayımı — fee=0. Bu varsayım gerçek execution'da test edilmemiş.

## Known risks
1. **Fee=0 varsayımı kanıtlanmamış.** paper_trader fee_engine'i kullanmıyor. Gerçek maker order'larda fee sıfır mı, negatif mi (rebate), yoksa pozitif mi — bilinmiyor.
2. **Fill rate %100 varsayımı gerçekçi değil.** Paper modda her iki taraf anında fill ediliyor. Gerçekte maker order fill olmaması veya tek taraf fill olması (toxic fill) riski var.
3. **Token index sırası doğrulanmamış.** market_discovery.py token_ids[0]=UP, token_ids[1]=DOWN varsayıyor. Bu Gamma API'den gelen sıraya bağlı ve doğrulanmamış.
4. **Chainlink resolution truth yok.** Kazanan taraf Binance close ile belirleniyor ama Polymarket Chainlink ile resolve ediyor. İkisi arasında sapma olabilir.
5. **API key'ler boş.** config.json'da private_key, clob_api_key, clob_api_secret, clob_api_passphrase hepsi boş. Gerçek execution için gerekli.
6. **polymarket_feed.py'de yoğun debug print'leri.** Production'da gürültü yaratır.
7. **Diğer branch'lerdeki kod ile senkronizasyon yok.** harness branch'teki fee_math.py, maker_shadow.py, taker_shadow.py bu branch'te mevcut değil. Hangi branch'in canonical olduğu belirsiz.
8. **pair_sum < 0.95 çok agresif olabilir.** Gerçek piyasada pair_sum genellikle ~0.98-1.02 civarında. 0.95 altı nadir; eşik çok düşükse hiç trade açılmaz, çok yüksekse edge negatif olur.
9. **Tek pencerede tek trade.** İlk sinyalden sonra skip — fırsat kaçırma vs erken giriş trade-off'u ölçülmemiş.

## Likely broken assumptions
1. **"Fee yok çünkü maker/post-only"** — Polymarket maker fee'si dönemsel olarak değişebilir. Hardcode 0 kabul etmek doctrine'ın "never hardcode fee rate" kuralını ihlal ediyor.
2. **"Her iki taraf fill olur"** — Gerçekte tek taraf fill, kısmi fill, veya hiç fill olmama durumları var. Tek taraf fill olursa strateji kırılır (payout garantisi bozulur).
3. **"Binance close ≈ Chainlink resolution"** — Chainlink'in resolution timestamp'i ile Binance REST çağrı anı arasında fark olabilir. Edge case'lerde (tam sınırda fiyat) farklı sonuç verebilir.
4. **"token_ids[0] her zaman UP"** — Gamma API'nin döndürdüğü sıra garanti değil.
5. **"pair_sum < 1 her zaman kârlı"** — Fee dahil edilince pair_sum < 1 bile zarar üretebilir.

## What is still unproven
1. Gerçek piyasada pair_sum < 0.95 ne sıklıkla oluşuyor — hiç gözlemlenmemiş.
2. Maker order fill rate — ölçülmemiş.
3. Fill toxicity (tek taraf fill riski) — ölçülmemiş.
4. Chainlink vs Binance resolution uyumu — karşılaştırılmamış.
5. Gerçek fee yapısı (maker rebate var mı, ne kadar) — sorgulanmamış.
6. Orderbook depth ve spread'in gün içi/haftalık paterni — analiz edilmemiş.
7. Bot'un canlı ortamda bağlantı stabilitesi (WS reconnect, Gamma API rate limit) — test edilmemiş.
8. 30 USDC bankroll ile minimum viable trade size'ın yeterliliği — hesaplanmamış.
9. config.json'daki parametre kombinasyonunun (entry window, target_sum, spread limit) optimalliği — backtest yok.
10. Diğer branch'lerdeki kodun (maker_shadow, taker_shadow, verdict_report) bu branch ile nasıl birleşeceği — tanımsız.

## Recommended next task
**Measurement-first yaklaşım: Observation-only mode ile gerçek piyasa verisini kaydet.**

Öncelik sırası:
1. resolution_truth.py yaz — Chainlink resolution sonucunu sorgula ve logla.
2. paper_trader'a fee_engine entegre et — fee=0 varsayımını kaldır.
3. Observation run: bot'u `--once` modunda çalıştır, pair_sum dağılımını, spread/depth istatistiklerini, ve Chainlink vs Binance uyumunu kaydet.
4. Kayıtlı veriyi analiz et (analyzer.py) — pair_sum < 0.95 gerçekte oluşuyor mu, fee dahil edge pozitif mi?

Strateji değişikliği yapılmamalı. Önce ölçüm katmanı kurulmalı.
