#!/usr/bin/env python3
"""
Polymarket Durum Teshisi
Sorunun 'approval eksik' mi yoksa 'buy hic dolmuyor mu' oldugunu anlatiyor.
"""
import json, sys, asyncio, aiohttp

def load_cfg(path="config.json"):
    with open(path) as f:
        return json.load(f)

async def main():
    cfg   = load_cfg(sys.argv[1] if len(sys.argv) > 1 else "config.json")
    creds = cfg["credentials"]
    net   = cfg["network"]

    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

    funder = creds.get("wallet_address", "")
    client = ClobClient(
        host=net["clob_url"],
        chain_id=net["chain_id"],
        key=creds["private_key"],
        creds=ApiCreds(
            api_key=creds["api_key"],
            api_secret=creds["api_secret"],
            api_passphrase=creds["api_passphrase"],
        ),
        funder=funder if funder else None,
        signature_type=1 if funder else 0,
    )

    print(f"\n{'='*55}")
    print(f"Cüzdan (funder)   : {funder or '(yok, EOA modu)'}")
    print(f"API key adresi    : {client.get_address()}")
    print(f"{'='*55}\n")

    # 1. COLLATERAL (USDC) bakiye ve allowance
    print("── 1. USDC (COLLATERAL) durumu ──")
    try:
        r = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        print(f"   Ham yanit : {r}")
        bal = float(r.get("balance", 0)) / 1e6 if isinstance(r, dict) else 0
        alw = r.get("allowance", "?") if isinstance(r, dict) else "?"
        print(f"   Bakiye    : ${bal:.4f} USDC")
        print(f"   Allowance : {alw}")
    except Exception as e:
        print(f"   HATA: {e}")

    # 2. CONDITIONAL (ERC-1155) allowance
    print("\n── 2. CONDITIONAL (ERC-1155) durumu ──")
    try:
        r = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL)
        )
        print(f"   Ham yanit : {r}")
        alw = r.get("allowance", "?") if isinstance(r, dict) else "?"
        print(f"   Allowance : {alw}")
        if alw == "0" or alw == 0:
            print("   >>> SORUN BURADA: ERC-1155 allowance = 0 <<<")
            print("   >>> Approval hicbir zaman set edilmemis veya sifirlanmis <<<")
        else:
            print("   Allowance mevcut — sorun approval degil!")
    except Exception as e:
        print(f"   HATA: {e}")

    # 3. /balance-allowance/update ham HTTP yaniti
    print("\n── 3. update_balance_allowance() ham yaniti ──")
    try:
        r = client.update_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL)
        )
        print(f"   Yanit: {repr(r)}")
        if not r:
            print("   >>> BEKLENEN SORUN: SDK GET istegiyle bos yanit aliyor <<<")
            print("   >>> Bu endpoint Magic.link proxy icin calismiyor <<<")
    except Exception as e:
        print(f"   HATA: {e}")

    # 4. Son islemlere bak (buy fill oldu mu?)
    print("\n── 4. Son 5 islem (buy fill kontrolu) ──")
    try:
        from py_clob_client.clob_types import TradeParams
        trades = client.get_trades(params=TradeParams(), next_cursor="MA==")
        if not trades:
            print("   Hic islem yok!")
        else:
            for t in trades[:5]:
                side   = t.get("side", "?")
                status = t.get("status", "?")
                size   = t.get("size", "?")
                price  = t.get("price", "?")
                ts     = t.get("created_at", "?")
                print(f"   {side:4s} | size={size} | price={price} | status={status} | {ts}")
    except Exception as e:
        print(f"   HATA: {e}")

    # 5. Acik pozisyonlar
    print("\n── 5. Mevcut acik pozisyonlar ──")
    try:
        from py_clob_client.clob_types import OpenOrderParams
        orders = client.get_orders(params=OpenOrderParams())
        if not orders:
            print("   Acik emir yok")
        else:
            for o in orders:
                print(f"   {o.get('side','?')} | size={o.get('original_size','?')} | "
                      f"filled={o.get('size_filled','?')} | status={o.get('status','?')}")
    except Exception as e:
        print(f"   HATA: {e}")

    print(f"\n{'='*55}\n")

asyncio.run(main())
