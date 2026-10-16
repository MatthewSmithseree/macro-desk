"""Minimal Gemini client (REST) that returns parsed JSON.

Uses the "lite" models: fast and reliable on the free tier. If one model is busy it moves to the
next, and it stops calling Gemini altogether once the time budget is spent, so a slow day at
Google never holds up the site refresh.
"""

from __future__ import annotations

import json
import logging
import os
import time

import requests

log = logging.getLogger("gemini")
MODELS = list(dict.fromkeys([os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
                             "gemini-3.1-flash-lite", "gemini-flash-lite-latest"]))
BUDGET_SECONDS = 240
_started = time.monotonic()


def ask_json(prompt: str, temperature: float = 0.2):
    """Send a prompt, get JSON back. Returns None on failure (the caller falls back)."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        log.warning("GEMINI_API_KEY not set; skipping AI step")
        return None
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
    }
    for model in MODELS:
        for attempt in range(2):
            if time.monotonic() - _started > BUDGET_SECONDS:
                log.warning("Gemini time budget used up; skipping the rest this run")
                return None
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": key}, json=body, timeout=60)
                if r.status_code in (429, 500, 503):
                    log.warning("Gemini %s busy (%s)", model, r.status_code)
                    time.sleep(3)
                    continue
                r.raise_for_status()
                return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
            except requests.Timeout:
                log.warning("Gemini %s timed out", model)
                break
            except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError) as e:
                log.warning("Gemini %s failed: %s", model, str(e)[:160])
                break
    return None
