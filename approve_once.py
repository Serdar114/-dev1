#!/usr/bin/env python3
"""
Polymarket CTF setApprovalForAll — Tek seferlik kalici onay scripti
====================================================================
Polygon Mainnet uzerinde CTF Exchange ve Neg Risk operatorleri icin
ERC-1155 setApprovalForAll cagrisini direkt on-chain yapar.

Kullanim:
    pip install web3
    python approve_once.py             # config.json kullanir
    python approve_once.py config.json # baska config
"""

import json
import os
import sys
import time

try:
    from web3 import Web3
    from eth_account import Account
except ImportError:
    print("HATA: 'pip install web3' komutunu calistirin.")
    sys.exit(1)

# ── Polymarket resmi kontract adresleri (Polygon Mainnet, chain_id=137) ──────
CTF_CONTRACT      = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # ERC-1155 conditional tokens
CTF_EXCHANGE      = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # CLOB exchange operator
NEG_RISK_ADAPTER  = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"  # Neg risk adapter operator

# Yedek RPC'ler (ilki basarisiz olursa siradakini dene)
RPC_URLS = [
    "https://polygon-rpc.com",
    "https://rpc-mainnet.maticvigil.com",
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
]

ERC1155_ABI = [
    {
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved",  "type": "bool"},
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "account",  "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def connect(rpc_urls: list) -> Web3:
    for url in rpc_urls:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                print(f"  RPC baglandi: {url}")
                return w3
        except Exception:
            pass
    raise ConnectionError("Hicbir RPC'ye baglanamadi. Internet veya RPC listesini kontrol edin.")


def load_cfg(path: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} bulunamadi!")
    with open(path) as f:
        return json.load(f)


def approve_operator(w3: Web3, ctf, owner: str, operator: str,
                     private_key: str, label: str) -> None:
    checksum_owner    = Web3.to_checksum_address(owner)
    checksum_operator = Web3.to_checksum_address(operator)

    # Mevcut durumu kontrol et
    already = ctf.functions.isApprovedForAll(checksum_owner, checksum_operator).call()
    if already:
        print(f"  [{label}] Zaten onaylanmis, islem gerekmez.")
        return

    print(f"  [{label}] Onay yok, setApprovalForAll gonderiliyor...")

    nonce    = w3.eth.get_transaction_count(checksum_owner)
    gas_px   = w3.eth.gas_price
    # Polygon'da hiz icin %20 fazla gas ver
    gas_px   = int(gas_px * 1.2)

    txn = ctf.functions.setApprovalForAll(checksum_operator, True).build_transaction({
        "from":     checksum_owner,
        "nonce":    nonce,
        "gas":      80_000,
        "gasPrice": gas_px,
        "chainId":  137,
    })

    signed = w3.eth.account.sign_transaction(txn, private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  [{label}] Gonderildi: 0x{tx_hash.hex()}")

    # Onay bekle (max 90s)
    receipt = None
    for attempt in range(18):
        time.sleep(5)
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            pass
        if receipt is not None:
            break
        print(f"  [{label}] Bekleniyor... ({(attempt+1)*5}s)")

    if receipt is None:
        print(f"  [{label}] UYARI: 90s icinde onay alınamadi, txn gonderildi ama henuz onaylanmadi.")
        print(f"            Hash: 0x{tx_hash.hex()} — daha sonra manuel kontrol edin.")
        return

    if receipt.status == 1:
        print(f"  [{label}] BASARILI! Blok: {receipt.blockNumber}")
    else:
        print(f"  [{label}] HATA: Txn revert oldu. Hash: 0x{tx_hash.hex()}")


def main() -> None:
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    print(f"\n=== Polymarket CTF Approval Script ===")
    print(f"Config: {cfg_path}\n")

    cfg   = load_cfg(cfg_path)
    creds = cfg["credentials"]
    pk    = creds["private_key"]

    # Private key'den adres turet
    derived_addr = Account.from_key(pk).address
    config_addr  = creds.get("wallet_address", "").lower()

    print(f"  Private key'den turetilen adres : {derived_addr}")
    print(f"  Config wallet_address            : {creds.get('wallet_address', '(yok)')}")

    # Hangi adresi kullanalim?
    if config_addr and config_addr != derived_addr.lower():
        print("\n  UYARI: private_key turetilen adres config wallet_address ile UYUSMUYOR.")
        print("  Bu bir proxy/API key kurulumu olabilir.")
        print("  setApprovalForAll *token sahibi* adresden gelmeli.")
        print("  Hangi adres conditional token tutuyor?")
        print("  (1) Private key'den turetilen:", derived_addr)
        print("  (2) Config wallet_address     :", creds.get("wallet_address"))
        choice = input("  Secim (1/2): ").strip()
        if choice == "2":
            print("\n  HATA: Config wallet_address icin private key'iniz yok.")
            print("  O adresin private key'ini girmeniz gerekiyor.")
            pk2 = input("  Wallet_address private key (0x...): ").strip()
            if not pk2.startswith("0x") or len(pk2) < 60:
                print("  Gecersiz private key, cikiliyor.")
                sys.exit(1)
            owner = creds["wallet_address"]
            pk    = pk2
        else:
            owner = derived_addr
    else:
        owner = derived_addr

    print(f"\n  Islemler gonderilecek adres: {owner}")

    # Baglanti
    w3 = connect(RPC_URLS)

    # MATIC bakiyesi kontrol
    balance_wei = w3.eth.get_balance(Web3.to_checksum_address(owner))
    balance_matic = float(w3.from_wei(balance_wei, "ether"))
    print(f"  MATIC bakiyesi: {balance_matic:.4f} MATIC")
    if balance_matic < 0.01:
        print("  UYARI: Cok az MATIC! Gas icin en az 0.01 MATIC gerekli.")
        print("  Cuzdan adresinize Polygon'dan MATIC gonderin, sonra tekrar calistirin.")
        sys.exit(1)

    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_CONTRACT),
        abi=ERC1155_ABI,
    )

    print("\n--- CTF Exchange operator onayi ---")
    approve_operator(w3, ctf, owner, CTF_EXCHANGE, pk, "CTF_EXCHANGE")

    print("\n--- Neg Risk Adapter operator onayi ---")
    approve_operator(w3, ctf, owner, NEG_RISK_ADAPTER, pk, "NEG_RISK_ADAPTER")

    print("\n=== Tamamlandi. Botu yeniden baslatabilirsiniz. ===\n")


if __name__ == "__main__":
    main()
