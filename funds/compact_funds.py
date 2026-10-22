"""Merge funds/data/funds.json with funds/profiles.json into compact chunks for the Fund Ledger Screener's data store.

Writes funds/build/meta.json and funds/build/chunk_XX.json (each well under the 256 KiB document limit).
"""
import json, os, sys, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from fetch import classify_expirations

SRC = os.path.join(HERE, "data", "funds.json")
PROFILES = os.path.join(HERE, "profiles.json")
OUT = os.path.join(HERE, "build")
PER_CHUNK = 12

# Starting estimates of how a fund's distributions are usually characterized for tax. The page lets the user edit these.
TAX_DEFAULTS = {
    "neos": {"roc": 0.85, "s1256": 0.15},
    "BDC": {"ord": 0.9, "ltcg": 0.08, "qual": 0.02},
    "bond": {"ord": 1.0},
}
TAX_MIX = {
    "QQQI": "neos", "SPYI": "neos", "NIHI": "neos", "IYRI": "neos", "MLPI": "neos", "BTCI": "neos",
    "GPIX": {"ord": 0.2, "qual": 0.2, "s1256": 0.3, "roc": 0.3},
    "IDVO": {"ord": 0.6, "qual": 0.2, "roc": 0.2}, "QDVO": {"ord": 0.6, "qual": 0.2, "roc": 0.2},
    "KGLD": {"ord": 0.5, "roc": 0.5}, "KSLV": {"ord": 0.5, "roc": 0.5}, "KQQQ": {"ord": 0.5, "roc": 0.5},
    "MSTY": {"ord": 0.3, "roc": 0.7},
    "PBDC": {"ord": 0.9, "qual": 0.1}, "CEFS": {"ord": 0.5, "ltcg": 0.2, "roc": 0.3},
    "PFFA": {"qual": 0.6, "ord": 0.3, "roc": 0.1},
    "PCN": "bond", "PTY": "bond", "PDI": "bond", "DSL": "bond", "ADAMH": "bond",
    "GOF": {"ord": 0.6, "roc": 0.4}, "FSCO": {"ord": 0.8, "roc": 0.2},
    "ADX": {"qual": 0.15, "ltcg": 0.8, "ord": 0.05},
    "NML": {"roc": 0.7, "qual": 0.3}, "EMO": {"roc": 1.0},
    "RLTY": {"reit": 0.5, "qual": 0.2, "roc": 0.3},
    "UTF": {"qual": 0.6, "ltcg": 0.2, "roc": 0.2}, "ASGI": {"qual": 0.5, "ltcg": 0.2, "roc": 0.3},
    "THQ": {"ltcg": 0.5, "roc": 0.4, "ord": 0.1},
}


def r(x, n=4):
    return round(x, n) if isinstance(x, (int, float)) else x


def tax_mix(t, prof):
    m = prof.get("taxMix") or TAX_MIX.get(t)
    if isinstance(m, str):
        m = TAX_DEFAULTS[m]
    if not m and prof.get("struct") == "BDC":
        m = TAX_DEFAULTS["BDC"]
    return m or {"ord": 1.0}


def compact(f, prof):
    t = f["ticker"]
    y = f.get("yahoo") or {}
    d = f.get("dist") or {}
    pf = f.get("perf") or {}
    nav = f.get("nav") or {}
    p = y.get("price")
    o = f.get("options") or {}
    exps = o.get("expirations") or []
    opt = {"f": classify_expirations([dt.date.fromisoformat(e) for e in exps]) if exps else o.get("frequency"), "x": exps[:10]}
    cc, cs = o.get("cc"), o.get("callSnapshot")
    if cc:
        opt["cc"] = {"f": cc["cycle"], "e": cc["expiration"], "d": cc["dte"], "ch": cc["chain"]}
    if cs:
        opt["cs"] = {"e": cs["expiration"], "d": cs["dte"], "k": cs["strike"], "b": cs["bid"], "a": cs["ask"],
                     "m": cs["mid"], "iv": cs["iv"], "oi": cs["openInterest"], "py": cs["premiumYield"], "ann": cs["annualized"]}
    struct = prof.get("struct") or ("ETF" if y.get("quoteType") == "ETF" else "Other")
    er = prof.get("er")
    er_src = "Sponsor via CEF Connect" if er is not None else None
    if er is None and y.get("netExpenseRatio") is not None:
        er, er_src = y["netExpenseRatio"] / 100, "Yahoo (net)"
    if er is None and (f.get("hold") or {}).get("erReport") is not None:
        er, er_src = f["hold"]["erReport"], "Yahoo (annual report)"
    navv = nav.get("v")
    prem = (p / navv - 1) if p and navv and struct not in ("Baby bond",) else None
    aum = y.get("totalAssets") or y.get("marketCap")
    rec = {
        "t": t, "n": prof.get("name") or y.get("longName") or y.get("shortName") or t,
        "st": struct, "grp": prof.get("grp") or ("Option income" if (y.get("category") or "") == "Derivative Income" else "Other"),
        "ty": prof.get("type") or y.get("category") or "Unclassified", "sty": prof.get("style"),
        "sp": prof.get("sponsor") or y.get("fundFamily"), "cat": y.get("category"), "inc": prof.get("incept") or y.get("fundInceptionDate"),
        "p": r(p, 2), "nav": r(navv, 4), "navD": nav.get("d"), "navSrc": nav.get("src"), "prem": r(prem),
        "disc": f.get("disc"), "aum": aum, "er": r(er, 5), "erSrc": er_src, "erAsOf": prof.get("erAsOf"),
        "lev": prof.get("lev"), "levPct": prof.get("levPct"), "pol": prof.get("policy"),
        "f": d.get("freq"), "per": d.get("perYear"), "last": d.get("last"), "lastEx": d.get("lastEx"), "reg": d.get("regular"),
        "fwd": d.get("fwd"), "ttm": d.get("ttm"), "pttm": d.get("prevTtm"), "cuts": d.get("cuts3y"),
        "dh": (d.get("hist") or [])[-80:], "dby": d.get("byYear") or {},
        "pf": {k: v for k, v in pf.items() if k not in ("monthly",) and v is not None}, "m": pf.get("monthly") or [],
        "nm": f.get("navMonthly") or [], "hold": f.get("hold"),
        "beta": r(y.get("beta3Year") or y.get("beta"), 2), "h52": y.get("fiftyTwoWeekHigh"), "l52": y.get("fiftyTwoWeekLow"),
        "ma50": r(y.get("fiftyDayAverage"), 2), "ma200": r(y.get("twoHundredDayAverage"), 2),
        "av": y.get("averageDailyVolume3Month") or y.get("averageVolume"), "o": opt,
        "mgr": prof.get("mgr"), "strat": prof.get("strat"), "tax": prof.get("tax"), "optx": prof.get("opt"), "bench": prof.get("bench"),
        "tx": tax_mix(t, prof), "src": prof.get("src"),
        "sum": (y.get("longBusinessSummary") or "")[:1200], "web": y.get("website"), "u": f.get("updated"),
    }
    if not prof:
        rec["noProf"] = 1
    if f.get("errors"):
        rec["err"] = f["errors"]
    return {k: v for k, v in rec.items() if v not in (None, "", [], {})}


def main():
    d = json.load(open(SRC))
    profiles = json.load(open(PROFILES))
    os.makedirs(OUT, exist_ok=True)
    for fn in os.listdir(OUT):
        os.remove(os.path.join(OUT, fn))
    funds = [compact(f, profiles.get(f["ticker"]) or {}) for f in d["funds"]]
    missing = [f["t"] for f in funds if not f.get("p")]
    funds = [f for f in funds if f.get("p")]
    chunks = [funds[i:i + PER_CHUNK] for i in range(0, len(funds), PER_CHUNK)]
    for i, c in enumerate(chunks):
        with open(f"{OUT}/chunk_{i:02d}.json", "w") as fh:
            json.dump({"funds": c}, fh, separators=(",", ":"))
    with open(f"{OUT}/meta.json", "w") as fh:
        json.dump({"generated": d["generated"], "count": len(funds), "chunks": len(chunks),
                   "tickers": sorted(f["t"] for f in funds), "notFound": missing,
                   "noProfile": sorted(f["t"] for f in funds if f.get("noProf"))}, fh)
    sizes = [os.path.getsize(f"{OUT}/chunk_{i:02d}.json") for i in range(len(chunks))]
    print(f"{len(funds)} funds in {len(chunks)} chunks, largest {max(sizes)//1024 if sizes else 0} KiB; not found: {missing}")


if __name__ == "__main__":
    main()
