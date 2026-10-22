import json, sys, requests, yfinance as yf, traceback
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36", "Accept": "application/json"}
def show(label, x, n=1500):
    s = x if isinstance(x, str) else json.dumps(x, default=str)
    print(f"==== {label}\n{s[:n]}\n", flush=True)
for t in ["SPYI", "ADX", "ARCC", "ADAMH", "MSTY", "PFFA"]:
    try:
        tk = yf.Ticker(t); info = tk.info or {}
        show(f"{t} info", {k: v for k, v in info.items() if k not in ("companyOfficers",)}, 4000)
        if info.get("quoteType") == "ETF":
            fd = tk.funds_data
            for a in ["description", "fund_overview", "fund_operations", "asset_classes", "top_holdings", "equity_holdings", "bond_holdings", "bond_ratings", "sector_weightings"]:
                try:
                    v = getattr(fd, a)
                    show(f"{t} funds_data.{a}", v.to_dict() if hasattr(v, "to_dict") else v, 1500)
                except Exception as e:
                    show(f"{t} funds_data.{a} ERR", str(e))
    except Exception:
        traceback.print_exc()
urls = [
 "https://www.cefconnect.com/api/v3/DailyPricing?props=Ticker,Name,DistributionRateNAV,LastUpdated,Discount,DistributionRatePrice,ReturnOnNAV,CategoryId,CategoryName,IsManagedDistribution,Price,PriceChange,NAV,NAVPublished,Cusip/",
 "https://www.cefconnect.com/api/v3/fund/ADX",
 "https://www.cefconnect.com/api/v3/funds/ADX",
 "https://www.cefconnect.com/api/v3/pricinghistory/ADX/1Y",
 "https://www.cefconnect.com/api/v3/distributionhistory/fund/ADX/01-01-2024/10-22-2026",
 "https://www.cefconnect.com/fund/ADX",
]
for u in urls:
    try:
        r = requests.get(u, headers=H, timeout=30)
        show(f"{u} -> {r.status_code} {r.headers.get('content-type')}", r.text, 2500)
    except Exception as e:
        show(f"{u} ERR", str(e))
