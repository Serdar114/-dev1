#!/usr/bin/env python3
"""
Polymarket API credentials türetici — tek seferlik çalıştır.
config.json'daki private_key'i kullanarak CLOB API kimlik bilgilerini
otomatik türetir ve config.json'a yazar.
"""
import json
import sys

try:
    from py_clob_client.client import ClobClient
except ImportError:
    print("HATA: pip install py_clob_client")
    sys.exit(1)

CFG_PATH = "config.json"

with open(CFG_PATH) as f:
    cfg = json.load(f)

pk   = cfg["credentials"]["private_key"]
host = cfg["network"]["clob_url"]
cid  = cfg["network"]["chain_id"]

print(f"Host     : {host}")
print(f"Chain ID : {cid}")
print("API credentials türetiliyor...")

client = ClobClient(host=host, chain_id=cid, key=pk)
creds  = client.create_or_derive_api_creds()

cfg["credentials"]["api_key"]        = creds.api_key
cfg["credentials"]["api_secret"]     = creds.api_secret
cfg["credentials"]["api_passphrase"] = creds.api_passphrase

with open(CFG_PATH, "w") as f:
    json.dump(cfg, f, indent=2)

print(f"OK — api_key: {creds.api_key}")
print("config.json güncellendi. Botu başlatabilirsin.")
