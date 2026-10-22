"""
Pull market data for the fund screener (ETFs, closed-end funds, BDCs, baby bonds).

Sources
  - Yahoo Finance (yfinance): price, NAV, assets, expense ratio, distributions, price history,
    holdings and sector weights, option expirations and covered-call chains.
  - Yahoo NAV symbols (X<ticker>X) for closed-end fund NAV history, used for premium/discount history.
  - funds/profiles.json: curated category, style box, managers, strategy and tax notes (merged in compact_funds.py).

Usage
  python funds/fetch_funds.py              # every ticker in funds/watchlist.txt
  python funds/fetch_funds.py SPYI ADX     # refresh only these, merge into funds/data/funds.json
"""
import json, math, os, sys, time, statistics, traceback, datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import pandas as pd
import yfinance as yf
from fetch import clean, rnd, options_profile  # shared helpers from the stock pipeline

OUT = os.path.join(HERE, "data", "funds.json")
WATCHLIST = os.path.join(HERE, "watchlist.txt")
TODAY = dt.date.today()

INFO_KEYS = [
    "quoteType", "longName", "shortName", "category", "fundFamily", "legalType", "fundInceptionDate",
    "currentPrice", "regularMarketPrice", "previousClose", "navPrice", "bookValue", "priceToBook",
    "totalAssets", "marketCap", "netExpenseRatio", "annualReportExpenseRatio", "yield", "dividendYield",
    "dividendRate", "trailingAnnualDividendRate", "lastDividendValue", "lastDividendDate", "exDividendDate",
    "beta", "beta3Year", "ytdReturn", "threeYearAverageReturn", "fiveYearAverageReturn",
    "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "fiftyDayAverage", "twoHundredDayAverage",
    "averageVolume", "averageDailyVolume3Month", "sharesOutstanding", "trailingPE", "debtToEquity",
    "returnOnEquity", "payoutRatio", "industry", "sector", "website", "longBusinessSummary",
]


def read_watchlist():
    if not os.path.exists(WATCHLIST):
        return []
    out = []
    for ln in open(WATCHLIST):
        ln = ln.strip().upper()
        if ln and not ln.startswith("#"):
            out.append(ln)
    return sorted(set(out))


def ts_date(x):
    try:
        return dt.datetime.utcfromtimestamp(int(x)).date().isoformat()
    except Exception:
        return None


def frequency(dates):
    """Payment frequency from the median gap between recent ex-dates."""
    if len(dates) < 2:
        return None, None
    recent = dates[-8:]
    gaps = [(b - a).days for a, b in zip(recent, recent[1:]) if (b - a).days > 0]
    if not gaps:
        return None, None
    g = statistics.median(gaps)
    if g <= 9:
        return "Weekly", 52
    if g <= 45:
        return "Monthly", 12
    if g <= 110:
        return "Quarterly", 4
    if g <= 220:
        return "Semiannual", 2
    return "Annual", 1


def distributions(tk, price):
    out = {}
    try:
        dv = tk.dividends
    except Exception:
        dv = None
    if dv is None or not len(dv):
        return out
    dv = dv[dv > 0]
    idx = [d.date() for d in dv.index]
    vals = [float(v) for v in dv.values]
    freq, per = frequency(idx)
    out["freq"], out["perYear"] = freq, per
    out["hist"] = [[d.isoformat(), round(v, 5)] for d, v in zip(idx, vals) if d >= TODAY - dt.timedelta(days=365 * 6)]
    out["last"] = round(vals[-1], 5)
    out["lastEx"] = idx[-1].isoformat()
    cut = TODAY - dt.timedelta(days=365)
    ttm = sum(v for d, v in zip(idx, vals) if d > cut)
    prev = sum(v for d, v in zip(idx, vals) if cut - dt.timedelta(days=365) < d <= cut)
    out["ttm"] = round(ttm, 5)
    out["prevTtm"] = round(prev, 5) if prev else None
    if per:
        k = 3 if per >= 12 else 2 if per >= 4 else 1
        if per == 52:
            k = 4
        reg = statistics.median(vals[-k:])
        out["regular"] = round(reg, 5)
        out["fwd"] = round(reg * per, 5)
    by = {}
    for d, v in zip(idx, vals):
        if d.year >= TODAY.year - 10:
            by[d.year] = by.get(d.year, 0) + v
    out["byYear"] = {str(y): round(v, 4) for y, v in by.items()}
    # cuts: regular payment falling more than 5% versus the median of the prior three, over 3 years
    cuts = 0
    v3 = [(d, v) for d, v in zip(idx, vals) if d >= TODAY - dt.timedelta(days=365 * 3)]
    for i in range(3, len(v3)):
        base = statistics.median([x[1] for x in v3[i - 3:i]])
        if base and v3[i][1] < base * 0.95 and (i + 1 >= len(v3) or v3[i + 1][1] < base * 0.95):
            cuts += 1
    out["cuts3y"] = cuts
    return out


def history(tk):
    """Daily closes for 6 years: price-only and total-return (adjusted) series."""
    try:
        h = tk.history(period="6y", interval="1d", auto_adjust=False, actions=False)
    except Exception:
        return None
    if h is None or not len(h) or "Close" not in h:
        return None
    h = h[["Close", "Adj Close"]].dropna()
    h.index = [d.date() for d in h.index]
    return h


def perf(h):
    if h is None or len(h) < 5:
        return {}
    last_d = h.index[-1]
    c, a = h["Close"], h["Adj Close"]

    def back(days):
        target = last_d - dt.timedelta(days=days)
        s = h[h.index <= target]
        return s.iloc[-1] if len(s) and (target - s.index[-1]).days < 10 else None

    out = {}
    for lab, days, yrs in (("1y", 365, 1), ("3y", 365 * 3, 3), ("5y", 365 * 5, 5)):
        r = back(days)
        if r is not None:
            tr = a.iloc[-1] / r["Adj Close"] - 1
            pr = c.iloc[-1] / r["Close"] - 1
            out["tr" + lab] = rnd((1 + tr) ** (1 / yrs) - 1 if yrs > 1 else tr, 4)
            out["pr" + lab] = rnd((1 + pr) ** (1 / yrs) - 1 if yrs > 1 else pr, 4)
    ys = h[h.index < dt.date(last_d.year, 1, 1)]
    if len(ys):
        out["trYtd"] = rnd(a.iloc[-1] / ys["Adj Close"].iloc[-1] - 1, 4)
    one = a[a.index >= last_d - dt.timedelta(days=365)]
    rets = one.pct_change().dropna()
    if len(rets) > 30:
        out["vol"] = rnd(float(rets.std()) * math.sqrt(252), 4)
    three = a[a.index >= last_d - dt.timedelta(days=365 * 3)]
    if len(three) > 30:
        dd = (three / three.cummax() - 1).min()
        out["mdd3y"] = rnd(float(dd), 4)
    out["since"] = h.index[0].isoformat()
    # month-end series for charts: [YYYY-MM, close, adjusted close]
    m = h.groupby([(d.year, d.month) for d in h.index]).last()
    out["monthly"] = [[f"{y}-{mo:02d}", rnd(float(r["Close"]), 3), rnd(float(r["Adj Close"]), 3)] for (y, mo), r in m.iterrows()][-72:]
    return out


def nav_series(ticker):
    """Closed-end fund NAV history from Yahoo's X<ticker>X symbol, month-end plus latest."""
    try:
        h = yf.Ticker(f"X{ticker}X").history(period="6y", interval="1d", auto_adjust=False, actions=False)
    except Exception:
        return None
    if h is None or not len(h) or "Close" not in h:
        return None
    s = h["Close"].dropna()
    if not len(s):
        return None
    s.index = [d.date() for d in s.index]
    if (TODAY - s.index[-1]).days > 20:
        return None
    m = s.groupby([(d.year, d.month) for d in s.index]).last()
    return {"latest": rnd(float(s.iloc[-1]), 4), "date": s.index[-1].isoformat(),
            "daily": s, "monthly": [[f"{y}-{mo:02d}", rnd(float(v), 3)] for (y, mo), v in m.items()][-72:]}


def holdings(tk):
    out = {}
    try:
        fd = tk.funds_data
    except Exception:
        return out
    try:
        th = fd.top_holdings
        if th is not None and len(th):
            out["top"] = [[str(sym), str(r.get("Name") or sym), rnd(float(r.get("Holding Percent") or 0), 4)] for sym, r in th.iterrows()][:15]
    except Exception:
        pass
    try:
        sw = fd.sector_weightings or {}
        out["sectors"] = {k: rnd(float(v), 4) for k, v in sw.items() if v}
    except Exception:
        pass
    try:
        ac = fd.asset_classes or {}
        out["assets"] = {k.replace("Position", ""): rnd(float(v), 4) for k, v in ac.items() if v}
    except Exception:
        pass
    try:
        op = fd.fund_operations
        if op is not None and len(op.columns):
            col = op.columns[0]
            out["turnover"] = rnd(clean(op.loc["Annual Holdings Turnover", col]), 4) if "Annual Holdings Turnover" in op.index else None
            out["erReport"] = rnd(clean(op.loc["Annual Report Expense Ratio", col]), 5) if "Annual Report Expense Ratio" in op.index else None
    except Exception:
        pass
    try:
        eq = fd.equity_holdings
        if eq is not None and len(eq.columns):
            col = eq.columns[0]
            vals = {}
            for k, key in (("Price/Earnings", "pe"), ("Price/Book", "pb"), ("Price/Sales", "ps"), ("Price/Cashflow", "pcf")):
                v = clean(eq.loc[k, col]) if k in eq.index else None
                if v:
                    vals[key] = rnd(1 / v if v < 1 else v, 2)  # Yahoo serves these inverted for funds
            if vals:
                out["eq"] = vals
    except Exception:
        pass
    try:
        bh = fd.bond_holdings
        if bh is not None and len(bh.columns):
            col = bh.columns[0]
            dur = clean(bh.loc["Duration", col]) if "Duration" in bh.index else None
            mat = clean(bh.loc["Maturity", col]) if "Maturity" in bh.index else None
            if dur or mat:
                out["bond"] = {"dur": rnd(dur, 2), "mat": rnd(mat, 2)}
    except Exception:
        pass
    try:
        br = fd.bond_ratings or {}
        br = {k: rnd(float(v), 4) for k, v in br.items() if v}
        if br:
            out["ratings"] = br
    except Exception:
        pass
    return {k: v for k, v in out.items() if v not in (None, {}, [])}


def build(ticker):
    rec = {"ticker": ticker, "errors": []}
    tk = yf.Ticker(ticker)
    info = {}
    for attempt in range(4):
        try:
            info = tk.info or {}
            if info:
                break
        except Exception as e:
            time.sleep(10 * (attempt + 1) if "Rate" in type(e).__name__ or "Too Many" in str(e) else 2)
    y = {k: clean(info.get(k)) for k in INFO_KEYS}
    for k in ("fundInceptionDate", "lastDividendDate", "exDividendDate"):
        if y.get(k):
            y[k] = ts_date(y[k])
    price = y.get("currentPrice") or y.get("regularMarketPrice") or y.get("previousClose")
    y["price"] = price
    rec["yahoo"] = y
    h = history(tk)
    if not price and h is not None and len(h):
        price = y["price"] = rnd(float(h["Close"].iloc[-1]), 4)
    rec["perf"] = perf(h)
    rec["dist"] = distributions(tk, price)
    if y.get("quoteType") == "ETF":
        rec["hold"] = holdings(tk)
    # NAV: ETFs report navPrice; closed-end funds have an X<ticker>X NAV series; BDCs report book value (NAV) quarterly
    nav = None
    if y.get("quoteType") != "ETF":
        ns = nav_series(ticker)
        if ns:
            nav = {"v": ns["latest"], "d": ns["date"], "src": "Yahoo NAV series"}
            rec["navMonthly"] = ns["monthly"]
            if h is not None and len(h):
                px = h["Close"]
                joined = pd.concat([px, ns["daily"]], axis=1, join="inner").dropna()
                joined.columns = ["p", "n"]
                yr = joined[joined.index >= TODAY - dt.timedelta(days=365)]
                if len(yr) > 30:
                    disc = yr["p"] / yr["n"] - 1
                    cur = float(disc.iloc[-1])
                    rec["disc"] = {"avg1y": rnd(float(disc.mean()), 4), "sd1y": rnd(float(disc.std()), 4),
                                   "z": rnd((cur - float(disc.mean())) / float(disc.std()), 2) if float(disc.std()) > 0 else None,
                                   "lo1y": rnd(float(disc.min()), 4), "hi1y": rnd(float(disc.max()), 4)}
        elif y.get("bookValue"):
            nav = {"v": y["bookValue"], "src": "Book value per share (latest report)"}
    elif y.get("navPrice"):
        nav = {"v": y["navPrice"], "src": "Yahoo NAV"}
    rec["nav"] = nav
    try:
        rec["options"] = options_profile(tk, price)
    except Exception as e:
        rec["errors"].append(f"options: {e}")
    if not price:
        rec["errors"].append("no price found")
    rec["updated"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return rec


def main():
    only = [t.upper() for t in sys.argv[1:] if t.strip()]
    if len(only) == 1 and ("," in only[0] or " " in only[0]):
        only = [t.strip() for t in only[0].replace(",", " ").split() if t.strip()]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    existing = {}
    if os.path.exists(OUT):
        existing = {f["ticker"]: f for f in json.load(open(OUT)).get("funds", [])}
    wl = read_watchlist()
    targets = [t for t in only if t in wl] if only else wl
    skipped = [t for t in only if t not in wl]
    if skipped:
        print("Not in funds/watchlist.txt, skipped:", skipped)

    results = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(build, t): t for t in targets}
        for f in as_completed(futs):
            t = futs[f]
            try:
                results[t] = f.result()
            except Exception as e:
                traceback.print_exc()
                results[t] = {"ticker": t, "errors": [str(e)]}
    retry = [t for t, r in results.items() if not (r.get("yahoo") or {}).get("price")]
    if retry:
        print(f"Retrying {len(retry)} after a pause", flush=True)
        time.sleep(45)
        for t in retry:
            try:
                r = build(t)
                if (r.get("yahoo") or {}).get("price"):
                    results[t] = r
            except Exception:
                pass
            time.sleep(2)

    merged = dict(existing) if only else {}
    merged.update(results)
    merged = {t: v for t, v in merged.items() if t in wl}
    payload = {"generated": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "count": len(merged),
               "funds": sorted(merged.values(), key=lambda f: f["ticker"])}
    with open(OUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"), default=str)
    ok = sum(1 for f in merged.values() if (f.get("yahoo") or {}).get("price"))
    print(f"Wrote {OUT}: {len(merged)} funds, {ok} priced")


if __name__ == "__main__":
    main()
