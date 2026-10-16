"""Earnings calendar for the tracked companies: write site/data/earnings.json.

- Dates, EPS estimate and reported EPS: Yahoo Finance (works for US, Asian and European listings).
- Revenue estimate/actual and before-open/after-close timing: Finnhub (free tier, US listings only).
- Window: reports from the last 14 days and the next 45 days.
- Refreshed at most once an hour (it makes ~70 requests), otherwise the last published file is reused.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import yfinance as yf

from common import DATA, HEADERS, SITE_URL, load_env

log = logging.getLogger("earnings")
OUT = DATA / "earnings.json"
NOW = datetime.now(timezone.utc)
REFRESH_MINUTES = 60

AI = [
    ("NVDA", "Nvidia", "AI / chips"), ("MSFT", "Microsoft", "AI / cloud"), ("GOOGL", "Alphabet", "AI / cloud"),
    ("AMZN", "Amazon", "AI / cloud"), ("META", "Meta Platforms", "AI"), ("AVGO", "Broadcom", "AI / chips"),
    ("AMD", "AMD", "AI / chips"), ("ORCL", "Oracle", "AI / cloud"), ("PLTR", "Palantir", "AI / software"),
    ("MU", "Micron", "AI / memory"), ("AAPL", "Apple", "AI / devices"), ("TSLA", "Tesla", "AI / autonomy"),
    ("CRM", "Salesforce", "AI / software"), ("SAP", "SAP", "AI / software"), ("ARM", "Arm Holdings", "AI / chips"),
    ("MRVL", "Marvell", "AI / chips"), ("ANET", "Arista Networks", "AI / networking"),
    ("VRT", "Vertiv", "AI / data-centre power"), ("DELL", "Dell Technologies", "AI / servers"),
    ("ASML", "ASML", "AI / semis"), ("SU.PA", "Schneider Electric", "AI / data-centre power"),
    ("2330.TW", "TSMC", "AI / semis"), ("2317.TW", "Hon Hai (Foxconn)", "AI / servers"),
    ("2382.TW", "Quanta Computer", "AI / servers"), ("2454.TW", "MediaTek", "AI / chips"),
    ("005930.KS", "Samsung Electronics", "AI / memory"), ("000660.KS", "SK Hynix", "AI / memory"),
    ("0700.HK", "Tencent", "AI / platforms"), ("9988.HK", "Alibaba", "AI / cloud"), ("9888.HK", "Baidu", "AI"),
    ("0981.HK", "SMIC", "AI / semis"), ("9984.T", "SoftBank Group", "AI / investment"),
    ("8035.T", "Tokyo Electron", "AI / semis"), ("6857.T", "Advantest", "AI / semis"),
    ("6146.T", "Disco", "AI / semis"), ("5803.T", "Fujikura", "AI / data-centre"),
]
FIN = [
    ("JPM", "JPMorgan Chase", "Bank"), ("BAC", "Bank of America", "Bank"), ("GS", "Goldman Sachs", "Bank"),
    ("MS", "Morgan Stanley", "Bank"), ("WFC", "Wells Fargo", "Bank"), ("BLK", "BlackRock", "Asset manager"),
    ("0005.HK", "HSBC", "Bank"), ("1299.HK", "AIA", "Insurer"), ("HDFCBANK.NS", "HDFC Bank", "Bank"),
]


def num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else round(f, 2)
    except (TypeError, ValueError):
        return None


def yahoo_rows(sym: str) -> list[dict]:
    df = yf.Ticker(sym).get_earnings_dates(limit=8)
    if df is None or df.empty:
        return []
    out = []
    for ts, r in df.iterrows():
        t = pd.Timestamp(ts).tz_convert("UTC")
        if NOW - timedelta(days=14) <= t <= NOW + timedelta(days=45):
            out.append({"time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "eps_est": num(r.get("EPS Estimate")),
                        "eps_act": num(r.get("Reported EPS")), "surprise_pct": num(r.get("Surprise(%)"))})
    return out


def finnhub_row(sym: str, key: str) -> dict | None:
    r = requests.get("https://finnhub.io/api/v1/calendar/earnings", timeout=30, params={
        "symbol": sym, "token": key, "from": (NOW - timedelta(days=14)).strftime("%Y-%m-%d"),
        "to": (NOW + timedelta(days=45)).strftime("%Y-%m-%d")})
    r.raise_for_status()
    rows = r.json().get("earningsCalendar") or []
    return rows[0] if rows else None


def fmt_money(v, us: bool):
    if v is None:
        return None
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(v) >= div:
            return f'{"$" if us else ""}{v / div:.1f}{suf}'
    return f"{v:,.0f}"


def build() -> list[dict]:
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    out = []
    for group, rows in (("AI", AI), ("Financials", FIN)):
        for sym, name, theme in rows:
            us = "." not in sym
            try:
                ys = yahoo_rows(sym)
            except Exception as e:
                log.warning("Yahoo %s: %s", sym, str(e)[:120])
                ys = []
            fh = None
            if us and key:
                try:
                    fh = finnhub_row(sym, key)
                    time.sleep(1.1)  # free tier: 60 requests/minute
                except requests.RequestException as e:
                    log.warning("Finnhub %s: %s", sym, e)
            if not ys and fh:
                ys = [{"time": fh["date"] + "T12:00:00Z", "eps_est": num(fh.get("epsEstimate")),
                       "eps_act": num(fh.get("epsActual")), "surprise_pct": None}]
            for y in ys:
                row = {"symbol": sym, "name": name, "group": group, "theme": theme, "us": us, **y,
                       "when": "", "rev_est": None, "rev_act": None}
                if fh and fh.get("date") == y["time"][:10] or (fh and us and abs(
                        (pd.Timestamp(fh["date"], tz="UTC") - pd.Timestamp(y["time"])).days) <= 2):
                    row["when"] = {"bmo": "Before open", "amc": "After close", "dmh": "During market"}.get(fh.get("hour"), "")
                    row["rev_est"], row["rev_act"] = fmt_money(num(fh.get("revenueEstimate")), us), fmt_money(num(fh.get("revenueActual")), us)
                    if row["eps_est"] is None:
                        row["eps_est"] = num(fh.get("epsEstimate"))
                    if row["eps_act"] is None:
                        row["eps_act"] = num(fh.get("epsActual"))
                    rv_e, rv_a = num(fh.get("revenueEstimate")), num(fh.get("revenueActual"))
                    row["rev_surprise"] = None if rv_e is None or rv_a is None else (0 if abs(rv_a / rv_e - 1) < 0.005 else (1 if rv_a > rv_e else -1))
                if row["eps_act"] is not None and row["eps_est"] is not None:
                    d = row["eps_act"] - row["eps_est"]
                    row["eps_surprise"] = 0 if abs(d) < 0.005 else (1 if d > 0 else -1)
                out.append(row)
    return sorted(out, key=lambda r: r["time"])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_env()
    try:
        prev = requests.get(SITE_URL.rstrip("/") + "/data/earnings.json", headers=HEADERS, timeout=30).json()
        age = NOW - datetime.fromisoformat(prev["updated_utc"].replace("Z", "+00:00"))
        if age < timedelta(minutes=REFRESH_MINUTES) and os.environ.get("FORCE") != "1":
            OUT.write_text(json.dumps(prev, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            log.info("Earnings refreshed %d min ago; reusing", age.total_seconds() // 60)
            return
    except (requests.RequestException, ValueError, KeyError):
        pass
    rows = build()
    OUT.write_text(json.dumps({"updated_utc": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "rows": rows},
                              ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log.info("Wrote earnings.json: %d reports (%d AI, %d financials)", len(rows),
             sum(r["group"] == "AI" for r in rows), sum(r["group"] == "Financials" for r in rows))


if __name__ == "__main__":
    main()
