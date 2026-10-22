"""
Pull S&P 500 data for the stock screener.

Sources
  - Constituents (ticker, name, GICS sector/sub-industry, CIK): github.com/datasets/s-and-p-500-companies
  - SEC XBRL company facts: 10-year annual EPS, equity, shares, operating cash flow, capex, revenue, net income
  - Yahoo Finance (yfinance): price, ratios, dividends, splits, option expirations, near-month call premium
  - FRED DAAA: Moody's Seasoned Aaa corporate bond yield (Graham "Y")

Usage
  python fetch.py                 # full refresh, all constituents
  python fetch.py AAPL MSFT KO    # refresh only these tickers, merge into data/stocks.json
"""
import json, math, os, sys, time, datetime as dt, io, csv, traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import yfinance as yf

UA = {"User-Agent": "Brandon Dreadin EA LLC brandon@dreadintax.com"}
OUT = "data/stocks.json"
TODAY = dt.date.today()


def clean(x):
    if x is None:
        return None
    try:
        if isinstance(x, (int, float)) and (math.isnan(x) or math.isinf(x)):
            return None
    except Exception:
        pass
    if hasattr(x, "item"):
        try:
            return clean(x.item())
        except Exception:
            return None
    return x


def rnd(x, n=4):
    x = clean(x)
    return round(x, n) if isinstance(x, (int, float)) else x


# ---------------------------------------------------------------- constituents
def constituents():
    url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    out = []
    for row in rows:
        out.append({
            "ticker": row["Symbol"].strip(),
            "name": row["Security"].strip(),
            "sector": row.get("GICS Sector", "").strip(),
            "subIndustry": row.get("GICS Sub-Industry", "").strip(),
            "hq": row.get("Headquarters Location", "").strip(),
            "cik": str(row.get("CIK", "")).strip().zfill(10),
            "founded": row.get("Founded", "").strip(),
        })
    return out


# ---------------------------------------------------------------- FRED Aaa
def aaa_yield():
    try:
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DAAA", headers=UA, timeout=30)
        r.raise_for_status()
        lines = [l.split(",") for l in r.text.strip().splitlines()[1:]]
        for d, v in reversed(lines):
            try:
                return {"value": float(v), "date": d}
            except ValueError:
                continue
    except Exception as e:
        print("FRED failed:", e)
    return None


# ---------------------------------------------------------------- SEC
FLOW_TAGS = {
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"],
    "netIncome": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
              "PaymentsForCapitalImprovements"],
    "dilutedShares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "dps": ["CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"],
}
INSTANT_TAGS = {
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "sharesOut": ["CommonStockSharesOutstanding"],
}


def _days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def sec_annual(facts, tags, instant=False, fy_ends=None):
    """Return {fiscal_year_end_date: (value, filed_date)} from 10-K filings; latest filing wins."""
    best = {}
    gaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        node = gaap.get(tag)
        if not node:
            continue
        found = {}
        for unit, items in node.get("units", {}).items():
            for it in items:
                if it.get("form") not in ("10-K", "10-K/A", "20-F", "40-F"):
                    continue
                end = it.get("end")
                if not end:
                    continue
                if instant:
                    if "start" in it:
                        continue
                    if fy_ends is not None and end not in fy_ends:
                        continue
                else:
                    if "start" not in it:
                        continue
                    d = _days(it["start"], end)
                    if d < 330 or d > 400:
                        continue
                prev = found.get(end)
                if prev is None or it["filed"] > prev[1]:
                    found[end] = (it["val"], it["filed"])
        for k, v in found.items():
            best.setdefault(k, v)  # first tag in priority order wins per period
    return best


def split_factor_after(splits, filed):
    """Cumulative split ratio for splits effective after the filing date."""
    f = 1.0
    for d, ratio in splits:
        if d > filed:
            f *= ratio
    return f


def sec_history(cik, splits, fallback_shares=None):
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=60)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            facts = r.json()
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 + attempt * 3)

    flows = {k: sec_annual(facts, v) for k, v in FLOW_TAGS.items()}
    fy_ends = set()
    for k in ("netIncome", "revenue", "ocf"):
        fy_ends |= set(flows[k].keys())
    inst = {k: sec_annual(facts, v, instant=True, fy_ends=fy_ends) for k, v in INSTANT_TAGS.items()}

    ends = sorted(fy_ends)[-12:]
    hist = []
    for end in ends:
        row = {"fyEnd": end}
        for k in ("netIncome", "revenue", "ocf", "capex"):
            v = flows[k].get(end)
            row[k] = v[0] if v else None
        # per-share and share counts, adjusted for splits after the filing date
        for k in ("eps", "dps"):
            v = flows[k].get(end)
            row[k] = round(v[0] / split_factor_after(splits, v[1]), 4) if v else None
        v = flows["dilutedShares"].get(end)
        row["dilutedShares"] = v[0] * split_factor_after(splits, v[1]) if v else None
        v = inst["sharesOut"].get(end)
        row["sharesOut"] = v[0] * split_factor_after(splits, v[1]) if v else None
        v = inst["equity"].get(end)
        row["equity"] = v[0] if v else None
        sh = row["sharesOut"] or row["dilutedShares"]
        if not sh and fallback_shares:
            # No share counts in XBRL (e.g. multi-class filers): approximate with today's share count
            sh = fallback_shares
            row["sharesApprox"] = True
            if row["eps"] is None and row["netIncome"] is not None:
                row["eps"] = round(row["netIncome"] / sh, 4)
        row["bvps"] = round(row["equity"] / sh, 4) if row["equity"] is not None and sh else None
        if row["ocf"] is not None:
            row["fcf"] = row["ocf"] - (row["capex"] or 0)
            row["fcfps"] = round(row["fcf"] / sh, 4) if sh else None
        else:
            row["fcf"] = row["fcfps"] = None
        hist.append(row)
    # keep the last 11 fiscal years (10 growth intervals)
    return hist[-11:]


# ---------------------------------------------------------------- Yahoo
INFO_KEYS = [
    "currentPrice", "regularMarketPrice", "previousClose", "marketCap", "enterpriseValue", "beta",
    "trailingPE", "forwardPE", "priceToBook", "priceToSalesTrailing12Months", "trailingPegRatio", "pegRatio",
    "dividendRate", "dividendYield", "trailingAnnualDividendRate", "payoutRatio", "fiveYearAvgDividendYield",
    "returnOnEquity", "returnOnAssets", "debtToEquity", "currentRatio", "quickRatio",
    "grossMargins", "operatingMargins", "profitMargins", "ebitdaMargins", "enterpriseToEbitda", "enterpriseToRevenue",
    "freeCashflow", "operatingCashflow", "totalCash", "totalDebt", "totalRevenue", "ebitda",
    "trailingEps", "forwardEps", "bookValue", "sharesOutstanding", "impliedSharesOutstanding", "revenueGrowth", "earningsGrowth",
    "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "fiftyDayAverage", "twoHundredDayAverage",
    "targetMeanPrice", "targetLowPrice", "targetHighPrice", "recommendationKey", "numberOfAnalystOpinions",
    "industry", "sector", "fullTimeEmployees", "website", "longBusinessSummary", "exDividendDate",
]


def options_profile(tk, price):
    try:
        exps = list(tk.options or [])
    except Exception:
        exps = []
    if not exps:
        return {"frequency": "None", "expirations": []}
    dates = sorted(dt.date.fromisoformat(e) for e in exps)
    near = [d for d in dates if (d - TODAY).days <= 70]
    gaps = [(b - a).days for a, b in zip(near, near[1:])]
    if gaps and min(gaps) <= 8 and sum(1 for g in gaps if g <= 8) >= 2:
        freq = "Weekly"
    else:
        months = {(d.year, d.month) for d in dates if (d - TODAY).days <= 120}
        freq = "Monthly" if len(months) >= 3 else "Quarterly"
    out = {"frequency": freq, "expirations": [d.isoformat() for d in dates[:12]], "count": len(dates)}

    # Near-month covered-call snapshot: expiration closest to 30 days, first strike at or above price
    try:
        if price:
            target = min(dates, key=lambda d: abs((d - TODAY).days - 30))
            ch = tk.option_chain(target.isoformat()).calls
            ch = ch[ch["strike"] >= price].sort_values("strike")
            if len(ch):
                c = ch.iloc[0]
                bid, ask, last = clean(c.get("bid")), clean(c.get("ask")), clean(c.get("lastPrice"))
                mid = (bid + ask) / 2 if bid and ask else last
                dte = (target - TODAY).days
                out["callSnapshot"] = {
                    "expiration": target.isoformat(), "dte": dte, "strike": rnd(c["strike"], 2),
                    "bid": rnd(bid, 2), "ask": rnd(ask, 2), "mid": rnd(mid, 2),
                    "iv": rnd(c.get("impliedVolatility"), 4),
                    "openInterest": clean(int(c["openInterest"])) if not pd.isna(c.get("openInterest")) else None,
                    "premiumYield": rnd(mid / price, 4) if mid else None,
                    "annualized": rnd((mid / price) * 365 / dte, 4) if mid and dte > 0 else None,
                }
    except Exception as e:
        out["callSnapshotError"] = str(e)[:120]
    return out


def yahoo(ticker):
    ysym = ticker.replace(".", "-")
    tk = yf.Ticker(ysym)
    info = {}
    for attempt in range(4):
        try:
            info = tk.info or {}
            if info:
                break
        except Exception as e:
            if "Rate" in type(e).__name__ or "Too Many" in str(e):
                time.sleep(10 * (attempt + 1))
            else:
                time.sleep(2)
    data = {k: clean(info.get(k)) for k in INFO_KEYS}
    if data.get("exDividendDate"):
        try:
            data["exDividendDate"] = dt.datetime.utcfromtimestamp(int(data["exDividendDate"])).date().isoformat()
        except Exception:
            pass
    price = data.get("currentPrice") or data.get("regularMarketPrice") or data.get("previousClose")
    data["price"] = price

    # dividends by calendar year (Yahoo series is split-adjusted)
    try:
        dv = tk.dividends
        if dv is not None and len(dv):
            s = dv.groupby(dv.index.year).sum()
            data["dividendsByYear"] = {int(y): round(float(v), 4) for y, v in s.items() if y >= TODAY.year - 12}
            last12 = dv[dv.index >= (pd.Timestamp.now(tz=dv.index.tz) - pd.Timedelta(days=365))].sum()
            data["dividendsTTM"] = round(float(last12), 4)
    except Exception:
        pass

    splits = []
    try:
        sp = tk.splits
        if sp is not None and len(sp):
            splits = [(d.date().isoformat(), float(r)) for d, r in sp.items() if r and r > 0]
    except Exception:
        pass
    data["splits"] = splits
    data["options"] = options_profile(tk, price)
    return data


# ---------------------------------------------------------------- per ticker
def build(c):
    rec = dict(c)
    errs = []
    try:
        y = yahoo(c["ticker"])
        rec["yahoo"] = y
    except Exception as e:
        errs.append(f"yahoo: {e}")
        y = {"splits": []}
        rec["yahoo"] = None
    try:
        rec["history"] = sec_history(c["cik"], y.get("splits", []), y.get("impliedSharesOutstanding") or y.get("sharesOutstanding"))
    except Exception as e:
        errs.append(f"sec: {e}")
        rec["history"] = None
    rec["errors"] = errs
    rec["updated"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return rec


def main():
    only = [t.upper() for t in sys.argv[1:] if t.strip()]
    if len(only) == 1 and ("," in only[0] or " " in only[0]):
        only = [t.strip() for t in only[0].replace(",", " ").split() if t.strip()]

    os.makedirs("data", exist_ok=True)
    existing = {}
    if os.path.exists(OUT):
        with open(OUT) as f:
            prev = json.load(f)
        existing = {s["ticker"]: s for s in prev.get("stocks", [])}

    cons = constituents()
    by_t = {c["ticker"]: c for c in cons}
    targets = [by_t[t] for t in only if t in by_t] if only else cons
    missing = [t for t in only if t not in by_t]
    if missing:
        print("Not in S&P 500 list:", missing)

    results = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(build, c): c["ticker"] for c in targets}
        for i, f in enumerate(as_completed(futs), 1):
            t = futs[f]
            try:
                results[t] = f.result()
            except Exception as e:
                traceback.print_exc()
                results[t] = {**by_t[t], "errors": [str(e)]}
            if i % 25 == 0:
                print(f"{i}/{len(targets)}", flush=True)

    merged = {} if not only else dict(existing)
    for c in cons:  # keep index membership current on full runs
        if not only and c["ticker"] in results:
            merged[c["ticker"]] = results[c["ticker"]]
    if only:
        merged.update(results)

    payload = {
        "generated": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "aaaYield": aaa_yield(),
        "count": len(merged),
        "stocks": sorted(merged.values(), key=lambda s: s["ticker"]),
    }
    with open(OUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"), default=str)
    ok = sum(1 for s in merged.values() if s.get("yahoo") and s.get("history"))
    print(f"Wrote {OUT}: {len(merged)} stocks, {ok} with both Yahoo and SEC data")


if __name__ == "__main__":
    main()
