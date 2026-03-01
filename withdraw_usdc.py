#!/usr/bin/env python3
"""
Polymarket USDC Withdraw Script
================================
Polymarket CLOB'daki tüm USDC'yi MetaMask cüzdanına geri çeker.

Kullanım:
    python3 withdraw_usdc.py
"""

import json
import sys
import time

try:
    from web3 import Web3
except ImportError:
    print("HATA: pip install web3")
    sys.exit(1)

# ══ Polymarket Kontrat Adresleri (Polygon Mainnet) ═══════════════════════════
CTF_EXCHANGE     = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_EXCHANGE     = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
USDC_BRIDGED     = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e (bridged)
USDC_NATIVE      = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # Native USDC

RPC_URLS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon.meowrpc.com",
    "https://1rpc.io/matic",
    "https://polygon-rpc.com",
    "https://polygon.llamarpc.com",
    "https://polygon.drpc.org",
]

# CTF Exchange ABI — deposit / withdraw / balance sorgulama
# 'deposits' = public mapping getter, 'getDeposit' = bazı versiyonlarda
EXCHANGE_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "amount", "type": "uint256"}],
        "name": "withdraw",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "deposits",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_ABI = [
    {
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def connect_rpc() -> Web3:
    for url in RPC_URLS:
        try:
            print(f"  RPC deneniyor: {url}")
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
            if w3.eth.chain_id == 137:
                print(f"  Baglandi (chain_id=137): {url}\n")
                return w3
        except Exception as e:
            print(f"  HATA: {e}")
    raise ConnectionError("Hicbir Polygon RPC'ye baglanılamadı.")


def get_deposit(w3: Web3, exchange_addr: str, wallet: str) -> int:
    """Exchange kontratındaki USDC bakiyesi (ham integer)."""
    try:
        exchange = w3.eth.contract(
            address=Web3.to_checksum_address(exchange_addr), abi=EXCHANGE_ABI
        )
        return exchange.functions.deposits(Web3.to_checksum_address(wallet)).call()
    except Exception:
        return 0


def get_token_balance(w3: Web3, token_addr: str, wallet: str) -> int:
    """ERC-20 token bakiyesi (ham integer)."""
    try:
        token = w3.eth.contract(
            address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI
        )
        return token.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
    except Exception:
        return 0


def send_withdraw(w3: Web3, exchange_addr: str, amount: int,
                  owner: str, pk: str, label: str) -> bool:
    """Exchange'den USDC cek."""
    try:
        exchange = w3.eth.contract(
            address=Web3.to_checksum_address(exchange_addr), abi=EXCHANGE_ABI
        )
        nonce  = w3.eth.get_transaction_count(Web3.to_checksum_address(owner))
        gas_px = int(w3.eth.gas_price * 1.3)

        txn = exchange.functions.withdraw(amount).build_transaction({
            "from":     Web3.to_checksum_address(owner),
            "nonce":    nonce,
            "gas":      120_000,
            "gasPrice": gas_px,
            "chainId":  137,
        })

        signed  = w3.eth.account.sign_transaction(txn, private_key=pk)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"\n  [{label}] Islem gonderildi: 0x{tx_hash.hex()}")
        print(f"  [{label}] Polygonscan: https://polygonscan.com/tx/0x{tx_hash.hex()}")
        print(f"  [{label}] Onay bekleniyor...")

        for i in range(30):
            time.sleep(4)
            try:
                receipt = w3.eth.get_transaction_receipt(tx_hash)
                if receipt is not None:
                    if receipt.status == 1:
                        print(f"  [{label}] BASARILI! Blok: {receipt.blockNumber}")
                        return True
                    else:
                        print(f"  [{label}] Islem REVERT oldu.")
                        return False
            except Exception:
                pass
            print(f"  [{label}] Bekleniyor... ({(i+1)*4}s)")
        return False
    except Exception as e:
        print(f"  [{label}] HATA: {e}")
        return False


def check_clob_balance(cfg: dict) -> float:
    """Polymarket CLOB API uzerinden bakiye sorgula."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

        creds = cfg["credentials"]
        client = ClobClient(
            host=cfg["network"]["clob_url"],
            chain_id=cfg["network"]["chain_id"],
            key=creds["private_key"],
            creds=ApiCreds(
                api_key=creds["api_key"],
                api_secret=creds["api_secret"],
                api_passphrase=creds["api_passphrase"],
            ),
        )

        resp = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        print(f"  CLOB API ham yanit: {resp}")

        if isinstance(resp, dict):
            raw = resp.get("balance") or resp.get("allowance") or 0
            return float(raw) / 1e6
        return 0.0
    except Exception as e:
        print(f"  CLOB API sorgusu basarisiz: {e}")
        return -1.0


def cancel_all_open_orders(cfg: dict):
    """Acik emirleri iptal et."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        creds = cfg["credentials"]
        client = ClobClient(
            host=cfg["network"]["clob_url"],
            chain_id=cfg["network"]["chain_id"],
            key=creds["private_key"],
            creds=ApiCreds(
                api_key=creds["api_key"],
                api_secret=creds["api_secret"],
                api_passphrase=creds["api_passphrase"],
            ),
        )
        result = client.cancel_all()
        print(f"  Acik emirler iptal edildi: {result}")
    except Exception as e:
        print(f"  Emir iptal edilemedi (normal olabilir): {e}")


def main():
    print("\n" + "=" * 60)
    print("  Polymarket USDC Withdraw")
    print("=" * 60 + "\n")

    # Config oku
    try:
        cfg = json.load(open("config.json"))
    except Exception:
        print("HATA: config.json bulunamadı.")
        sys.exit(1)

    creds  = cfg["credentials"]
    pk     = creds["private_key"]
    wallet = Web3.to_checksum_address(creds["wallet_address"])

    print(f"Cuzdan  : {wallet}")
    print(f"Hedef   : {wallet} (MetaMask)\n")

    # 1. RPC
    print("── RPC Baglantisi ──────────────────────────────────────")
    w3 = connect_rpc()

    # 2. On-chain bakiyeler
    print("── On-Chain Bakiyeler ───────────────────────────────────")
    nat_bal   = get_token_balance(w3, USDC_NATIVE,  wallet) / 1e6
    bridg_bal = get_token_balance(w3, USDC_BRIDGED, wallet) / 1e6
    ctf_dep   = get_deposit(w3, CTF_EXCHANGE, wallet)       / 1e6
    neg_dep   = get_deposit(w3, NEG_EXCHANGE,  wallet)      / 1e6

    print(f"  Native USDC (cuzdan)    : ${nat_bal:.4f}")
    print(f"  Bridged USDC (cuzdan)   : ${bridg_bal:.4f}")
    print(f"  CTF Exchange deposit    : ${ctf_dep:.4f}")
    print(f"  Neg Risk Exchange dep.  : ${neg_dep:.4f}")

    # 3. CLOB API bakiyesi
    print("\n── CLOB API Bakiyesi ────────────────────────────────────")
    clob_bal = check_clob_balance(cfg)
    if clob_bal >= 0:
        print(f"  CLOB kolateral bakiyesi : ${clob_bal:.4f}")

    # 4. On-chain exchange toplam
    total = ctf_dep + neg_dep
    print(f"\n── Ozetlenen Bakiyeler ──────────────────────────────────")
    print(f"  CTF + NegRisk Exchange  : ${total:.4f}")
    if clob_bal > 0:
        print(f"  CLOB API bakiyesi       : ${clob_bal:.4f}")

    # 5. On-chain'de para varsa cek
    if total >= 0.01:
        print(f"\n  ${total:.4f} USDC cekilecek → {wallet}")
        ans = input("  Devam? (evet/hayir): ").strip().lower()
        if ans in ("evet", "e", "yes", "y"):
            cancel_all_open_orders(cfg)

            if ctf_dep > 0:
                print("\n── CTF Exchange Withdraw ──────────────────────────────")
                send_withdraw(w3, CTF_EXCHANGE, int(ctf_dep * 1e6), wallet, pk, "CTF")

            if neg_dep > 0:
                print("\n── Neg Risk Exchange Withdraw ─────────────────────────")
                send_withdraw(w3, NEG_EXCHANGE, int(neg_dep * 1e6), wallet, pk, "NegRisk")

            time.sleep(4)
            print("\n── Son Bakiye ─────────────────────────────────────────")
            print(f"  Native USDC  : ${get_token_balance(w3, USDC_NATIVE, wallet)/1e6:.4f}")
            print(f"  Bridged USDC : ${get_token_balance(w3, USDC_BRIDGED, wallet)/1e6:.4f}")
        else:
            print("  Iptal edildi.")

    else:
        # On-chain'de yok ama CLOB'da olabilir
        if clob_bal > 0:
            print(f"""
  PARA CLOB'DA (${clob_bal:.4f} USDC):
  On-chain exchange'de degil, Polymarket'in ic muhasebesinde tutuluyor.

  COZUM 1 — Web arayuzu (en kolay):
    1. polymarket.com adresine git
    2. MetaMask ile baglan (0x3Ff88... adresi)
    3. Profil → Portfolio → Withdraw
    4. Tutarı gir → Onayla

  COZUM 2 — CLOB API ile (asagida deneniyor)...
""")
            ans = input("  CLOB API uzerinden cekmeyi deneyelim mi? (evet/hayir): ").strip().lower()
            if ans in ("evet", "e", "yes", "y"):
                cancel_all_open_orders(cfg)
                print("  CLOB API'de dogrudan 'withdraw' endpoint'i mevcut degil.")
                print("  Lutfen polymarket.com/portfolio uzerinden Manuel Withdraw yap.")
        else:
            print("""
  TUM BAKIYELER $0.00
  ─────────────────────────────────────────────────────
  Muhtemel sebepler:
  1. CLOB API anahtar suresi dolmus olabilir (yenile)
  2. Para daha once cekilmis olabilir
  3. Hata kontrolu icin:
     https://polygonscan.com/address/0x3Ff88Cc4a2e27102e46C2Cc4aE73Ade5119dd286

  Kontrol etmek icin:
  → polymarket.com/portfolio adresini MetaMask ile ac
""")

    print("  Tamamlandi.\n")


if __name__ == "__main__":
    main()
