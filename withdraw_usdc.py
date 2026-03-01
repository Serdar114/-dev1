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
    from eth_account import Account
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
EXCHANGE_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "amount", "type": "uint256"}],
        "name": "deposit",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "amount", "type": "uint256"}],
        "name": "withdraw",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "depositor", "type": "address"}],
        "name": "getDeposit",
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
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def connect_rpc() -> Web3:
    for url in RPC_URLS:
        try:
            print(f"  RPC deneniyor: {url}")
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
            cid = w3.eth.chain_id
            if cid == 137:
                print(f"  ✓ Bağlandı (chain_id=137): {url}\n")
                return w3
        except Exception as e:
            print(f"  ✗ {e}")
    raise ConnectionError("Hiçbir Polygon RPC'ye bağlanılamadı.")


def get_deposit(w3: Web3, exchange_addr: str, wallet: str) -> int:
    """Exchange kontratındaki USDC bakiyesini sorgula (ham integer)."""
    try:
        exchange = w3.eth.contract(
            address=Web3.to_checksum_address(exchange_addr), abi=EXCHANGE_ABI
        )
        return exchange.functions.getDeposit(Web3.to_checksum_address(wallet)).call()
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
    """Exchange'den USDC çek."""
    try:
        exchange = w3.eth.contract(
            address=Web3.to_checksum_address(exchange_addr), abi=EXCHANGE_ABI
        )
        nonce    = w3.eth.get_transaction_count(Web3.to_checksum_address(owner))
        gas_px   = int(w3.eth.gas_price * 1.3)

        txn = exchange.functions.withdraw(amount).build_transaction({
            "from":     Web3.to_checksum_address(owner),
            "nonce":    nonce,
            "gas":      120_000,
            "gasPrice": gas_px,
            "chainId":  137,
        })

        signed  = w3.eth.account.sign_transaction(txn, private_key=pk)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"\n  [{label}] İşlem gönderildi: 0x{tx_hash.hex()}")
        print(f"  [{label}] Polygonscan: https://polygonscan.com/tx/0x{tx_hash.hex()}")
        print(f"  [{label}] Onay bekleniyor...")

        for i in range(30):
            time.sleep(4)
            try:
                receipt = w3.eth.get_transaction_receipt(tx_hash)
                if receipt is not None:
                    if receipt.status == 1:
                        print(f"  [{label}] ✓ BAŞARILI! Blok: {receipt.blockNumber}")
                        return True
                    else:
                        print(f"  [{label}] ✗ İşlem REVERT oldu.")
                        return False
            except Exception:
                pass
            print(f"  [{label}] Bekleniyor... ({(i+1)*4}s)")

        print(f"  [{label}] UYARI: 120s içinde onay alınamadı. Hash ile kontrol et.")
        return False

    except Exception as e:
        print(f"  [{label}] HATA: {e}")
        return False


def check_clob_balance(cfg: dict) -> float:
    """Polymarket CLOB API üzerinden bakiye sorgula."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

        creds = cfg["credentials"]
        client = ClobClient(
            host=cfg["network"]["clob_url"],
            chain_id=cfg["network"]["chain_id"],
            key=creds["private_key"],
            creds={
                "apiKey":       creds["api_key"],
                "secret":       creds["api_secret"],
                "passphrase":   creds["api_passphrase"],
            },
        )

        resp = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        raw = resp.get("balance", 0) if isinstance(resp, dict) else 0
        return float(raw)
    except Exception as e:
        print(f"  CLOB API sorgusu başarısız: {e}")
        return -1.0


def cancel_all_open_orders(cfg: dict):
    """Açık emirleri iptal et (bakiyeyi serbest bırakmak için)."""
    try:
        from py_clob_client.client import ClobClient
        creds = cfg["credentials"]
        client = ClobClient(
            host=cfg["network"]["clob_url"],
            chain_id=cfg["network"]["chain_id"],
            key=creds["private_key"],
            creds={
                "apiKey":       creds["api_key"],
                "secret":       creds["api_secret"],
                "passphrase":   creds["api_passphrase"],
            },
        )
        result = client.cancel_all()
        print(f"  Açık emirler iptal edildi: {result}")
    except Exception as e:
        print(f"  Emir iptal edilemedi (normal olabilir): {e}")


def main():
    print("\n" + "="*60)
    print("  Polymarket USDC Withdraw")
    print("="*60 + "\n")

    # Config oku
    try:
        cfg = json.load(open("config.json"))
    except Exception:
        print("HATA: config.json bulunamadı.")
        sys.exit(1)

    creds  = cfg["credentials"]
    pk     = creds["private_key"]
    wallet = Web3.to_checksum_address(creds["wallet_address"])

    print(f"Cüzdan  : {wallet}")
    print(f"Hedef   : {wallet} (aynı adres — MetaMask'ın)\n")

    # 1. RPC bağlantısı
    print("── RPC Bağlantısı ──────────────────────────────────────")
    w3 = connect_rpc()

    # 2. Mevcut bakiyeleri göster
    print("── Mevcut Bakiyeler ─────────────────────────────────────")

    nat_bal   = get_token_balance(w3, USDC_NATIVE,  wallet) / 1e6
    bridg_bal = get_token_balance(w3, USDC_BRIDGED, wallet) / 1e6
    ctf_dep   = get_deposit(w3, CTF_EXCHANGE, wallet)       / 1e6
    neg_dep   = get_deposit(w3, NEG_EXCHANGE,  wallet)      / 1e6

    print(f"  Native USDC (cüzdan)    : ${nat_bal:.4f}")
    print(f"  Bridged USDC (cüzdan)   : ${bridg_bal:.4f}")
    print(f"  CTF Exchange deposit    : ${ctf_dep:.4f}")
    print(f"  Neg Risk Exchange dep.  : ${neg_dep:.4f}")

    # 3. CLOB API bakiyesi
    print("\n── CLOB API Bakiyesi ────────────────────────────────────")
    clob_bal = check_clob_balance(cfg)
    if clob_bal >= 0:
        print(f"  CLOB kolateral bakiyesi : ${clob_bal:.4f}")
    else:
        print("  CLOB API'ye erişilemedi.")

    # 4. Çekilecek toplam
    total = ctf_dep + neg_dep
    print(f"\n── Çekilebilir Toplam ───────────────────────────────────")
    print(f"  CTF + NegRisk Exchange  : ${total:.4f}")

    if total < 0.01:
        print("\n  ℹ Exchange kontratlarında çekilecek bakiye yok.")
        print("  Eğer CLOB bakiyesi görünüyorsa Polymarket web sitesinden")
        print("  'Withdraw' butonunu kullan: https://polymarket.com/portfolio")
        print("\n  İşlem yapılmadı.\n")
        return

    # 5. Onay al
    print(f"\n  ${total:.4f} USDC çekilecek → {wallet}")
    ans = input("  Devam etmek istiyor musun? (evet/hayır): ").strip().lower()
    if ans not in ("evet", "e", "yes", "y"):
        print("  İptal edildi.\n")
        return

    # 6. Açık emirleri iptal et
    print("\n── Açık Emirler İptal Ediliyor ──────────────────────────")
    cancel_all_open_orders(cfg)

    # 7. CTF Exchange'den çek
    if ctf_dep > 0:
        print("\n── CTF Exchange Withdraw ─────────────────────────────────")
        raw_amount = int(ctf_dep * 1e6)
        send_withdraw(w3, CTF_EXCHANGE, raw_amount, wallet, pk, "CTF")

    # 8. Neg Risk Exchange'den çek
    if neg_dep > 0:
        print("\n── Neg Risk Exchange Withdraw ────────────────────────────")
        raw_amount = int(neg_dep * 1e6)
        send_withdraw(w3, NEG_EXCHANGE, raw_amount, wallet, pk, "NegRisk")

    # 9. Son bakiye
    time.sleep(4)
    print("\n── Son Bakiye ────────────────────────────────────────────")
    new_nat   = get_token_balance(w3, USDC_NATIVE,  wallet) / 1e6
    new_bridg = get_token_balance(w3, USDC_BRIDGED, wallet) / 1e6
    print(f"  Native USDC (cüzdan)    : ${new_nat:.4f}")
    print(f"  Bridged USDC (cüzdan)   : ${new_bridg:.4f}")
    print("\n  Tamamlandı.\n")


if __name__ == "__main__":
    main()
