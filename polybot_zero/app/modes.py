"""
modes.py — Mode constants and mode-gating helpers.

MEASUREMENT_ONLY: observe, record hypotheticals, no positions opened
SELECTIVE_PAPER:  open paper positions on proven buckets only
"""

MEASUREMENT_ONLY = "measurement_only"
SELECTIVE_PAPER  = "selective_paper"

VALID_MODES = {MEASUREMENT_ONLY, SELECTIVE_PAPER}


def validate_mode(mode: str) -> str:
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode: {mode!r}. Must be one of: {VALID_MODES}")
    return mode


def is_paper_active(mode: str) -> bool:
    return mode == SELECTIVE_PAPER
