# Macro Desk

A personal cross-asset dashboard: markets, top headlines, news by region, economic calendar, central banks and research.

## How it works

- `scripts/fetch_markets.py` pulls free data (Yahoo Finance, US Treasury, Bundesbank, Bank of England, Japan MoF, FRED) and writes `site/data/markets.json`.
- `site/index.html` is the whole website. It reads the JSON files in `site/data/`.
- `.github/workflows/update.yml` runs the scripts every 15 minutes on GitHub and publishes `site/` to GitHub Pages.

## Run it on your computer

```
.venv\Scripts\python.exe scripts\fetch_markets.py
.venv\Scripts\python.exe -m http.server 8765 --directory site
```

Then open http://localhost:8765.

## API keys

Keys live in `.env` on your computer (never committed) and as GitHub repository secrets:
`FRED_API_KEY`, `FINNHUB_API_KEY`, `GEMINI_API_KEY`.
