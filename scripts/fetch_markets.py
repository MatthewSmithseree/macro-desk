"""Fetch market data for the Markets tab and write site/data/markets.json.

Sources (all free, no API key needed):
  - Yahoo Finance via yfinance: equity indices, FX, commodities, crypto (about 15 min delayed)
  - US Treasury: daily par yield curve
  - Deutsche Bundesbank: daily Bund yields
  - Bank of England: daily gilt par yields (5y, 10y, 20y)
  - Japan Ministry of Finance: daily JGB yields
  - FRED (St. Louis Fed): ICE BofA credit spreads (option-adjusted, daily)

Run it with:  .venv\\Scripts\\python.exe scripts\\fetch_markets.py
"""

from __future__ import annotations

import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from common import load_env

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "site" / "data" / "markets.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (macro-desk personal dashboard)"}
TIMEOUT = 90

PERIODS = {"1D": None, "1W": 7, "1M": 30, "6M": 182, "1Y": 365}
MAX_SPARK_POINTS = 60

log = logging.getLogger("markets")


# --------------------------------------------------------------------------- instruments

EQUITIES = [
    ("S&P 500", "^GSPC"), ("Nasdaq Composite", "^IXIC"), ("Euro Stoxx 50", "^STOXX50E"),
    ("Nikkei 225", "^N225"), ("Hang Seng", "^HSI"), ("KOSPI", "^KS11"),
    ("Nifty 50", "^NSEI"), ("SSE Composite", "000001.SS"),
]
FX = [
    ("USD/JPY", "JPY=X", 2), ("Dollar Index", "DX-Y.NYB", 2), ("USD/HKD", "HKD=X", 4),
    ("EUR/USD", "EURUSD=X", 4), ("AUD/USD", "AUDUSD=X", 4), ("USD/KRW", "KRW=X", 2),
    # Yahoo has no reliable offshore CNH series, so onshore CNY stands in for now.
    ("USD/CNY (onshore)", "CNY=X", 4),
]
COMMODITIES = [
    ("Gold", "GC=F", 2), ("Brent crude", "BZ=F", 2), ("WTI crude", "CL=F", 2), ("Bitcoin", "BTC-USD", 0),
]
CREDIT = [  # FRED series id, values are in percent
    ("US Investment Grade", "BAMLC0A0CM"), ("US BBB", "BAMLC0A4CBBB"),
    ("US High Yield", "BAMLH0A0HYM2"), ("US CCC & lower", "BAMLH0A3HYC"),
    ("Euro High Yield", "BAMLHE00EHYIOAS"), ("Asia EM Corporate", "BAMLEMRACRPIASIAOAS"),
]


# --------------------------------------------------------------------------- helpers

def value_at_or_before(s: pd.Series, when: pd.Timestamp):
    s = s[s.index <= when]
    return None if s.empty else float(s.iloc[-1])


def thin(values: list[float], n: int = MAX_SPARK_POINTS) -> list[float]:
    if len(values) <= n:
        return values
    step = (len(values) - 1) / (n - 1)
    return [values[round(i * step)] for i in range(n)]


def summarise(s: pd.Series, kind: str, intraday: pd.Series | None = None) -> dict:
    """Latest value, change over each period, and sparkline points.

    kind: "pct" (prices, % change), "yield" (yields in %, change in bp), "bp" (levels in bp, change in bp)
    """
    s = s.dropna().sort_index()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    s = s[~s.index.duplicated(keep="last")]
    last, last_date = float(s.iloc[-1]), s.index[-1]

    def change(past):
        if past is None:
            return None
        if kind == "pct":
            return round((last / past - 1) * 100, 2)
        if kind == "yield":
            return round((last - past) * 100, 1)
        return round(last - past, 1)

    chg, spark = {}, {}
    for p, days in PERIODS.items():
        if days is None:
            chg[p] = change(float(s.iloc[-2])) if len(s) > 1 else None
            pts = intraday.dropna().tolist() if intraday is not None and len(intraday) > 3 else s.iloc[-5:].tolist()
        else:
            start = last_date - timedelta(days=days)
            chg[p] = change(value_at_or_before(s, start))
            pts = s[s.index >= start].tolist()
        spark[p] = [round(float(v), 4) for v in thin(pts)]
    return {"last": round(last, 4), "asof": last_date.strftime("%Y-%m-%d"), "chg": chg, "spark": spark}


def get(url: str, **params) -> requests.Response:
    r = requests.get(url, params=params or None, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r


# --------------------------------------------------------------------------- sources

def yahoo(symbols: list[str]) -> tuple[dict, dict]:
    """Daily closes for one year plus the latest session's 15-minute bars."""
    daily = yf.download(symbols, period="13mo", interval="1d", group_by="ticker",
                        auto_adjust=False, progress=False, threads=False)
    intra = yf.download(symbols, period="5d", interval="15m", group_by="ticker",
                        auto_adjust=False, progress=False, threads=False)
    closes, sessions = {}, {}
    for sym in symbols:
        try:
            c = daily[sym]["Close"].dropna()
            if c.empty:  # retry once on its own; Yahoo sometimes drops a symbol from a batch
                c = yf.Ticker(sym).history(period="13mo")["Close"].dropna()
            closes[sym] = c
        except KeyError:
            log.warning("Yahoo: no daily data for %s", sym)
        try:
            i = intra[sym]["Close"].dropna()
            if not i.empty:
                sessions[sym] = i[i.index.date == i.index[-1].date()]
        except KeyError:
            pass
    return closes, sessions


def us_treasury() -> pd.DataFrame:
    """Daily par yield curve for this year and last year. Columns are tenors like '2 Yr'."""
    frames = []
    year = datetime.now().year
    for y in (year - 1, year):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               f"daily-treasury-rates.csv/{y}/all")
        r = get(url, type="daily_treasury_yield_curve", field_tdr_date_value=y, _format="csv")
        frames.append(pd.read_csv(io.StringIO(r.text), parse_dates=["Date"], index_col="Date"))
    return pd.concat(frames).sort_index()


def bundesbank(maturities=("01", "02", "03", "05", "07", "10", "15", "20", "30")) -> pd.DataFrame:
    start = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
    cols = {}
    for m in maturities:
        key = f"BBSIS/D.I.ZST.ZI.EUR.S1311.B.A604.R{m}XX.R.A.A._Z._Z.A"
        r = get(f"https://api.statistiken.bundesbank.de/rest/data/{key}", format="csv", lang="en", startPeriod=start)
        rows = [line.split(",")[:2] for line in r.text.splitlines() if line[:2] == "20"]
        s = pd.Series({pd.Timestamp(d): float(v) for d, v in rows if v not in (".", "")})
        cols[f"{int(m)}Y"] = s
    return pd.DataFrame(cols).sort_index()


def bank_of_england() -> pd.DataFrame:
    codes = {"5Y": "IUDSNPY", "10Y": "IUDMNPY", "20Y": "IUDLNPY"}
    start = (datetime.now() - timedelta(days=400)).strftime("%d/%b/%Y")
    cols = {}
    for tenor, code in codes.items():
        r = get("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp",
                **{"csv.x": "yes", "Datefrom": start, "Dateto": "now", "SeriesCodes": code,
                   "CSVF": "TN", "UsingCodes": "Y", "VPD": "Y", "VFD": "N"})
        df = pd.read_csv(io.StringIO(r.text))
        cols[tenor] = pd.Series(df.iloc[:, 1].values, index=pd.to_datetime(df.iloc[:, 0], format="%d %b %Y"))
    return pd.DataFrame(cols).sort_index()


def japan_mof() -> pd.DataFrame:
    """JGB yields. The 'all' file has full history; the monthly file is the fallback."""
    base = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
    frames = []
    for path in ("historical/jgbcme_all.csv", "jgbcme.csv"):
        try:
            text = get(base + path).content.decode("shift_jis", errors="replace")
        except requests.RequestException as e:
            log.warning("MoF %s failed: %s", path, e)
            continue
        df = pd.read_csv(io.StringIO(text), skiprows=1)
        df = df[pd.to_datetime(df["Date"], errors="coerce").notna()]
        df.index = pd.to_datetime(df["Date"])
        frames.append(df.drop(columns="Date").apply(pd.to_numeric, errors="coerce"))
    if not frames:
        raise RuntimeError("No JGB data")
    df = pd.concat(frames).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df[df.index >= datetime.now() - timedelta(days=400)]


def fred(series_id: str) -> pd.Series:
    """Official FRED API (needs FRED_API_KEY); the public CSV link is only a fallback because it
    often times out from GitHub's servers."""
    start = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
    key = os.environ.get("FRED_API_KEY", "").strip()
    if key:
        r = get("https://api.stlouisfed.org/fred/series/observations", series_id=series_id, api_key=key,
                file_type="json", observation_start=start)
        obs = [(o["date"], o["value"]) for o in r.json()["observations"] if o["value"] not in (".", "")]
        return pd.Series([float(v) for _, v in obs], index=pd.to_datetime([d for d, _ in obs]))
    r = get("https://fred.stlouisfed.org/graph/fredgraph.csv", id=series_id, cosd=start)
    df = pd.read_csv(io.StringIO(r.text))
    s = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    s.index = pd.to_datetime(df.iloc[:, 0])
    return s.dropna()


# --------------------------------------------------------------------------- build

def item(name, symbol, decimals, series, kind, intraday=None):
    try:
        return {"name": name, "symbol": symbol, "decimals": decimals, **summarise(series, kind, intraday)}
    except Exception as e:  # keep going: one bad series shouldn't break the page
        log.warning("Could not summarise %s: %s", name, e)
        return {"name": name, "symbol": symbol, "decimals": decimals, "error": "unavailable"}


def curve(df: pd.DataFrame, tenors: list[str], labels: list[str]) -> dict:
    df = df[tenors].dropna(how="any")
    today = df.iloc[-1]
    out = {"tenors": labels, "asof": df.index[-1].strftime("%Y-%m-%d"),
           "today": [round(float(v), 3) for v in today], "ago": {}}
    for p, days in PERIODS.items():
        past = df.iloc[-2] if days is None else df[df.index <= df.index[-1] - timedelta(days=days)].iloc[-1]
        out["ago"][p] = [round(float(v), 3) for v in past]
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_env()
    groups, curves, errors = [], {}, []

    # Prices from Yahoo
    syms = [s for _, s in EQUITIES] + [s for _, s, _ in FX] + [s for _, s, _ in COMMODITIES]
    log.info("Yahoo Finance: %d symbols", len(syms))
    closes, sessions = yahoo(syms)

    def price_items(rows):
        out = []
        for row in rows:
            name, sym = row[0], row[1]
            dec = row[2] if len(row) > 2 else 2
            if sym not in closes:
                out.append({"name": name, "symbol": sym, "decimals": dec, "error": "unavailable"})
                continue
            out.append(item(name, sym, dec, closes[sym], "pct", sessions.get(sym)))
        return out

    groups.append({"tab": "Equities", "name": "Equities", "unit": "price", "source": "Yahoo Finance (about 15 min delayed)",
                   "items": price_items(EQUITIES)})

    # Government bonds
    bonds, spreads = [], []
    try:
        log.info("US Treasury yield curve")
        ust = us_treasury()
        for label, col in [("US 2Y", "2 Yr"), ("US 5Y", "5 Yr"), ("US 10Y", "10 Yr"), ("US 30Y", "30 Yr")]:
            bonds.append(item(label, f"UST {col}", 3, ust[col], "yield"))
        spreads.append(item("US 2s10s", "10Y minus 2Y", 0, (ust["10 Yr"] - ust["2 Yr"]) * 100, "bp"))
        spreads.append(item("US 5s30s", "30Y minus 5Y", 0, (ust["30 Yr"] - ust["5 Yr"]) * 100, "bp"))
        curves["US"] = curve(ust, ["3 Mo", "6 Mo", "1 Yr", "2 Yr", "3 Yr", "5 Yr", "7 Yr", "10 Yr", "20 Yr", "30 Yr"],
                             ["3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y"])
    except Exception as e:
        errors.append(f"US Treasury: {e}")
    for label, fetch, col, cv in [
        ("Germany 10Y (Bund)", bundesbank, "10Y", (["1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y"], "DE")),
        ("UK 10Y (Gilt)", bank_of_england, "10Y", (["5Y", "10Y", "20Y"], "UK")),
        ("Japan 10Y (JGB)", japan_mof, "10Y", (["1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y", "40Y"], "JP")),
    ]:
        try:
            log.info(label)
            df = fetch()
            bonds.append(item(label, label.split(" (")[0], 3, df[col], "yield"))
            curves[cv[1]] = curve(df, cv[0], cv[0])
        except Exception as e:
            errors.append(f"{label}: {e}")
            bonds.append({"name": label, "symbol": "", "decimals": 3, "error": "unavailable"})

    groups.append({"tab": "Rates", "name": "Government bonds", "unit": "yield", "curve": True,
                   "source": "US Treasury, Bundesbank, Bank of England, Japan Ministry of Finance (daily close)", "items": bonds})
    groups.append({"tab": "Rates", "name": "Yield spreads", "unit": "bp",
                   "source": "Calculated from US Treasury yields", "items": spreads})

    groups.append({"tab": "FX", "name": "FX", "unit": "price", "source": "Yahoo Finance (about 15 min delayed)",
                   "items": price_items(FX)})

    # Credit spreads (FRED gives percent; convert to basis points)
    credit = []
    for name, sid in CREDIT:
        try:
            log.info("FRED %s", sid)
            credit.append(item(name, sid, 0, fred(sid) * 100, "bp"))
        except Exception as e:
            errors.append(f"FRED {sid}: {e}")
            credit.append({"name": name, "symbol": sid, "decimals": 0, "error": "unavailable"})
    groups.append({"tab": "Credit", "name": "Credit spreads", "unit": "bp",
                   "source": "FRED, ICE BofA option-adjusted spreads (daily, about 1-day lag)", "items": credit})

    groups.append({"tab": "Commodities", "name": "Commodities & crypto", "unit": "price",
                   "source": "Yahoo Finance (futures front month for oil and gold)", "items": price_items(COMMODITIES)})

    # Anything that failed this run: show the last good value instead of "unavailable".
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text(encoding="utf-8"))
            for k, c in prev.get("curves", {}).items():
                curves.setdefault(k, {**c, "stale": True})
            last = {(g["name"], i["name"]): i for g in prev["groups"] for i in g["items"] if "error" not in i}
            for g in groups:
                g["items"] = [({**last[(g["name"], i["name"])], "stale": True}
                               if "error" in i and (g["name"], i["name"]) in last else i) for i in g["items"]]
        except (ValueError, KeyError) as e:
            log.warning("Could not read previous markets.json: %s", e)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "groups": groups, "curves": curves, "errors": errors}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    n_ok = sum(1 for g in groups for i in g["items"] if "error" not in i)
    n_all = sum(len(g["items"]) for g in groups)
    log.info("Wrote %s: %d/%d instruments, curves: %s", OUT.relative_to(ROOT), n_ok, n_all, ", ".join(curves))
    for e in errors:
        log.warning(e)


if __name__ == "__main__":
    main()
