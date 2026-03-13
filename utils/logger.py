"""
utils/logger.py — V21 tek logger sistemi.

Kullanım:
    import utils.logger as logger
    logger.setup(cfg)          # main'de bir kez çağır
    log = logger.get("module") # her modülde
    log.info("mesaj")
"""

import logging
import sys
from pathlib import Path

_initialized = False
_log_file = "bot_log_v21.txt"


def setup(cfg: dict | None = None, level: int = logging.INFO) -> None:
    """
    Root logger'ı kurar. Yalnızca bir kez çağrılmalı.
    cfg["log_file"] varsa o dosyaya yazar, yoksa bot_log_v21.txt.
    Dashboard konsolu kirletmesin diye stderr handler sadece WARNING+ basar.
    """
    global _initialized, _log_file

    if cfg:
        _log_file = cfg.get("log_file", _log_file)

    if _initialized:
        return
    _initialized = True

    fmt_file = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fmt_stderr = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    # ── File handler (INFO ve üstü) ─────────────────────────────────────────
    fh = logging.FileHandler(_log_file, encoding="utf-8", mode="a")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt_file)
    root.addHandler(fh)

    # ── Stderr handler (WARNING ve üstü — dashboard'u kirletmez) ───────────
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(fmt_stderr)
    root.addHandler(sh)

    # Gürültülü kütüphaneleri sustur
    for noisy in ("websockets", "aiohttp", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root.info("=== V21 Logger başlatıldı | log_file=%s ===", _log_file)


def get(name: str) -> logging.Logger:
    """İsimlendirilmiş logger döndürür."""
    return logging.getLogger(name)
