"""Convert data/stocks.json into compact chunks for the screener page's data store.

Writes build/meta.json and build/chunk_XX.json (each well under the 256 KiB document limit).
"""
import json, os, sys, datetime as dt
from fetch import classify_expirations, fix_shares

SRC = sys.argv[1] if len(sys.argv) > 1 else "data/stocks.json"
OUT = "build"
PER_CHUNK = 35


def r(x, n=4):
    return round(x, n) if isinstance(x, (int, float)) else x


def compact(s):
    y = s.get("yahoo") or {}
    H = s.get("history") or []
    hist = []
    ref = y.get("impliedSharesOutstanding") or y.get("sharesOutstanding")
    for h in H:
        bvps, fcfps = h.get("bvps"), h.get("fcfps")
        if not h.get("sharesApprox"):
            sh = fix_shares(h.get("sharesOut"), ref) or fix_shares(h.get("dilutedShares"), ref)
            bvps = h["equity"] / sh if sh and h.get("equity") is not None else None
            fcfps = h["fcf"] / sh if sh and h.get("fcf") is not None else None
        row = {"e": h["fyEnd"], "rev": h.get("revenue"), "ni": h.get("netIncome"),
               "eps": r(h.get("eps")), "bv": r(bvps), "fcf": r(fcfps), "dps": r(h.get("dps"))}
        if h.get("sharesApprox"):
            row["a"] = 1
        hist.append({k: v for k, v in row.items() if v is not None})

    # current book value per share: Yahoo MRQ unless it disagrees wildly with the latest filing (share-class issues)
    bv = y.get("bookValue")
    last_bv = next((h["bv"] for h in reversed(hist) if h.get("bv")), None)
    if last_bv and (not bv or bv <= 0 or bv / last_bv > 5 or bv / last_bv < 0.2):
        bv = last_bv

    shares = y.get("impliedSharesOutstanding") or y.get("sharesOutstanding")
    eps = y.get("trailingEps")
    if eps is None and hist and hist[-1].get("eps") is not None:
        eps = hist[-1]["eps"]

    div = y.get("dividendsTTM")
    if not div:
        div = y.get("trailingAnnualDividendRate") or 0

    o = y.get("options") or {}
    cs = o.get("callSnapshot")
    exps = o.get("expirations") or []
    freq = classify_expirations([dt.date.fromisoformat(e) for e in exps]) if exps else o.get("frequency")
    opt = {"f": freq, "x": exps[:10]}
    if cs:
        opt["cs"] = {"e": cs["expiration"], "d": cs["dte"], "k": cs["strike"], "b": cs["bid"], "a": cs["ask"],
                     "m": cs["mid"], "iv": cs["iv"], "oi": cs["openInterest"], "py": cs["premiumYield"], "ann": cs["annualized"]}

    rat = {
        "pe": r(y.get("trailingPE"), 2), "fpe": r(y.get("forwardPE"), 2), "pb": r(y.get("priceToBook"), 2),
        "ps": r(y.get("priceToSalesTrailing12Months"), 2), "peg": r(y.get("trailingPegRatio") or y.get("pegRatio"), 2),
        "eve": r(y.get("enterpriseToEbitda"), 2), "gm": r(y.get("grossMargins")), "om": r(y.get("operatingMargins")),
        "nm": r(y.get("profitMargins")), "roe": r(y.get("returnOnEquity")), "roa": r(y.get("returnOnAssets")),
        "de": r(y.get("debtToEquity"), 2), "cr": r(y.get("currentRatio"), 2), "qr": r(y.get("quickRatio"), 2),
        "pay": r(y.get("payoutRatio")), "beta": r(y.get("beta"), 2), "h52": y.get("fiftyTwoWeekHigh"),
        "l52": y.get("fiftyTwoWeekLow"), "ma50": r(y.get("fiftyDayAverage"), 2), "ma200": r(y.get("twoHundredDayAverage"), 2),
        "tgt": y.get("targetMeanPrice"), "rec": y.get("recommendationKey"), "feps": y.get("forwardEps"),
        "cash": y.get("totalCash"), "debt": y.get("totalDebt"), "exd": y.get("exDividendDate"),
    }
    if rat["pb"] and bv and y.get("price") and abs(rat["pb"] - y["price"] / bv) / max(rat["pb"], 1e-9) > 0.8:
        rat["pb"] = r(y["price"] / bv, 2)

    rec = {
        "t": s["ticker"], "n": s["name"], "sec": s["sector"], "sub": s["subIndustry"], "hq": s.get("hq"),
        "cik": s.get("cik"), "fd": s.get("founded"), "ind": y.get("industry"),
        "p": r(y.get("price"), 2), "mc": y.get("marketCap"), "eps": r(eps), "bv": r(bv), "fcf": y.get("freeCashflow"),
        "sh": shares, "div": r(div), "dy": y.get("dividendsByYear") or {}, "h": hist, "o": opt,
        "r": {k: v for k, v in rat.items() if v is not None},
        "sum": (y.get("longBusinessSummary") or "")[:1400], "web": y.get("website"), "emp": y.get("fullTimeEmployees"),
        "u": s.get("updated"),
    }
    if s.get("errors"):
        rec["err"] = s["errors"]
    return {k: v for k, v in rec.items() if v not in (None, "", [])}


def main():
    d = json.load(open(SRC))
    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):
        os.remove(os.path.join(OUT, f))
    stocks = [compact(s) for s in d["stocks"]]
    stocks = [s for s in stocks if s.get("p")]
    chunks = [stocks[i:i + PER_CHUNK] for i in range(0, len(stocks), PER_CHUNK)]
    for i, c in enumerate(chunks):
        with open(f"{OUT}/chunk_{i:02d}.json", "w") as f:
            json.dump({"stocks": c}, f, separators=(",", ":"))
    with open(f"{OUT}/meta.json", "w") as f:
        json.dump({"generated": d["generated"], "aaa": d.get("aaaYield"), "count": len(stocks), "chunks": len(chunks)}, f)
    sizes = [os.path.getsize(f"{OUT}/chunk_{i:02d}.json") for i in range(len(chunks))]
    print(f"{len(stocks)} stocks in {len(chunks)} chunks, largest {max(sizes)//1024} KiB")


if __name__ == "__main__":
    main()
