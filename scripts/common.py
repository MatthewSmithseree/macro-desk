"""Shared helpers for the data scripts."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "site" / "data"
SITE_URL = os.environ.get("SITE_URL", "https://matthewsmithseree.github.io/macro-desk/")
HEADERS = {"User-Agent": "Mozilla/5.0 (macro-desk personal dashboard)"}


def load_env() -> None:
    """Read keys from .env when running on your computer. On GitHub they come from repository secrets."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
