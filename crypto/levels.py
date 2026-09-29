#!/usr/bin/env python3
"""Hitung support/resisten + metrik on-chain untuk koin crypto.

Hanya pakai standard library Python (tanpa pip install).

Contoh:
    python3 crypto/levels.py BTC
    python3 crypto/levels.py LINK ONDO TAO
    python3 crypto/levels.py BTC --price 83746      # pakai harga manual
    python3 crypto/levels.py BTC --no-onchain

Sumber data:
    - Harga & candle : Binance public API (tanpa key)
    - On-chain       : Coin Metrics Community API (tanpa key) -> MVRV,
                       realized price, active address, hashrate
"""

import argparse
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

BINANCE_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]
COINMETRICS = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
MVRV_BANDS = [1.0, 1.2, 1.42, 1.5, 1.62, 2.0, 2.4, 3.0]


# ---------------------------------------------------------------- HTTP

def _ssl_context():
    cafile = os.getenv("SSL_CERT_FILE") or os.getenv("REQUESTS_CA_BUNDLE")
    return ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()


def fetch_json(url, params=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "akhmad-levels/1.0"})
    with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as resp:
        return json.load(resp)


def binance(path, params):
    last_err = None
    for host in BINANCE_HOSTS:
        try:
            return fetch_json(host + path, params)
        except Exception as err:  # coba host berikutnya
            last_err = err
    raise RuntimeError(f"Binance tidak bisa diakses: {last_err}")


def klines(symbol, interval, limit):
    rows = binance("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    return [
        {"t": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
         "c": float(r[4]), "v": float(r[5])}
        for r in rows
    ]


def last_price(symbol):
    return float(binance("/api/v3/ticker/price", {"symbol": symbol})["price"])


# ---------------------------------------------------------- indikator

def sma(values, n):
    return sum(values[-n:]) / n if len(values) >= n else None


def ema(values, n):
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[:n], closes[1:n + 1]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    for a, b in zip(closes[n:], closes[n + 1:]):
        d = b - a
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
    return 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)


def atr(candles, n=14):
    trs = [
        max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"]))
        for p, c in zip(candles, candles[1:])
    ]
    return sum(trs[-n:]) / min(n, len(trs)) if trs else 0.0


def swing_points(candles, width=3):
    """Fractal: high/low tertinggi/terendah dibanding `width` candle kiri-kanan."""
    points = []
    for i in range(width, len(candles) - width):
        win = candles[i - width:i + width + 1]
        if candles[i]["h"] == max(c["h"] for c in win):
            points.append(candles[i]["h"])
        if candles[i]["l"] == min(c["l"] for c in win):
            points.append(candles[i]["l"])
    return points


def cluster(points, tol):
    """Gabungkan titik swing yang jaraknya < tol. Return [(level, jumlah_sentuhan)]."""
    groups = []
    for p in sorted(points):
        if groups and p - groups[-1][-1] <= tol:
            groups[-1].append(p)
        else:
            groups.append([p])
    return [(sum(g) / len(g), len(g)) for g in groups]


def volume_poc(candles, bins=40):
    """Point of Control: harga dengan volume terbesar (volume profile sederhana)."""
    lo = min(c["l"] for c in candles)
    hi = max(c["h"] for c in candles)
    if hi == lo:
        return lo
    step = (hi - lo) / bins
    vol = [0.0] * bins
    for c in candles:
        typical = (c["h"] + c["l"] + c["c"]) / 3
        vol[min(int((typical - lo) / step), bins - 1)] += c["v"]
    i = max(range(bins), key=vol.__getitem__)
    return lo + step * (i + 0.5)


# ------------------------------------------------------------ analisa

def analyze(symbol, price=None):
    c4h = klines(symbol, "4h", 500)
    c1d = klines(symbol, "1d", 400)
    price = price or last_price(symbol)
    closes_d = [c["c"] for c in c1d]

    tol = atr(c4h) * 0.6
    levels = {}
    for lvl, n in cluster(swing_points(c4h, 3), tol):
        levels[round(lvl, 8)] = levels.get(round(lvl, 8), 0) + n
    for lvl, n in cluster(swing_points(c1d, 3), tol):
        # swing harian bobotnya 2x
        levels[round(lvl, 8)] = levels.get(round(lvl, 8), 0) + 2 * n
    merged = cluster([l for l, n in levels.items() for _ in range(n)], tol)

    extra = {
        "SMA50 1D": sma(closes_d, 50),
        "SMA200 1D": sma(closes_d, 200),
        "EMA200 1D": ema(closes_d, 200),
        "EMA200 4H": ema([c["c"] for c in c4h], 200),
        "POC volume 4H": volume_poc(c4h[-180:]),
    }

    resist = sorted([(l, n) for l, n in merged if l > price])[:5]
    support = sorted([(l, n) for l, n in merged if l < price], reverse=True)[:5]
    return {
        "symbol": symbol,
        "price": price,
        "rsi_1d": rsi(closes_d),
        "rsi_4h": rsi([c["c"] for c in c4h]),
        "atr_4h": atr(c4h),
        "resist": resist,
        "support": support,
        "extra": extra,
        "high_30d": max(c["h"] for c in c1d[-30:]),
        "low_30d": min(c["l"] for c in c1d[-30:]),
    }


def onchain(asset, price=None, days=30):
    metrics = ["PriceUSD", "CapMVRVCur", "AdrActCnt"]
    if asset == "btc":
        metrics.append("HashRate")
    data = fetch_json(COINMETRICS, {
        "assets": asset, "metrics": ",".join(metrics),
        "frequency": "1d", "page_size": days, "paging_from": "end",
    })["data"]
    if not data:
        return None
    last = data[-1]
    mvrv = float(last["CapMVRVCur"])
    realized = float(last["PriceUSD"]) / mvrv
    price = price or float(last["PriceUSD"])
    addr = [float(d["AdrActCnt"]) for d in data if d.get("AdrActCnt")]
    half = len(addr) // 2
    out = {
        "date": last["time"][:10],
        "mvrv": price / realized,
        "realized": realized,
        "bands": [(b, realized * b) for b in MVRV_BANDS],
        "mvrv_range": (min(float(d["CapMVRVCur"]) for d in data),
                       max(float(d["CapMVRVCur"]) for d in data)),
        "addr_change": (sum(addr[half:]) / len(addr[half:])) / (sum(addr[:half]) / half) - 1 if half else None,
    }
    if "HashRate" in last:
        out["hashrate_zh"] = float(last["HashRate"]) / 1e9  # TH/s -> ZH/s
    return out


# ------------------------------------------------------------- output

def fmt(x):
    if x is None:
        return "-"
    if x >= 1000:
        return f"${x:,.0f}"
    if x >= 1:
        return f"${x:,.2f}"
    return f"${x:.4f}"


def pct(level, price):
    return f"{(level / price - 1) * 100:+.1f}%"


def report(a, oc=None):
    p = a["price"]
    print(f"\n{'=' * 52}\n {a['symbol']}  harga {fmt(p)}\n{'=' * 52}")
    print(f" RSI 1D {a['rsi_1d']:.1f} | RSI 4H {a['rsi_4h']:.1f} | ATR 4H {fmt(a['atr_4h'])}")
    print(f" Range 30 hari: {fmt(a['low_30d'])} - {fmt(a['high_30d'])}")

    print("\n RESISTEN (terdekat dulu)")
    for lvl, n in a["resist"]:
        print(f"   {fmt(lvl):>12}  {pct(lvl, p):>7}  kekuatan {'#' * min(n, 10)}")
    print(f"   {'>':>12}  harga di bawah ini")
    print(" SUPPORT (terdekat dulu)")
    for lvl, n in a["support"]:
        print(f"   {fmt(lvl):>12}  {pct(lvl, p):>7}  kekuatan {'#' * min(n, 10)}")

    print("\n MOVING AVERAGE / VOLUME")
    for name, val in a["extra"].items():
        if val:
            side = "support" if val < p else "resisten"
            print(f"   {name:<14} {fmt(val):>12}  {pct(val, p):>7}  ({side})")

    if oc:
        print(f"\n ON-CHAIN (Coin Metrics, data s/d {oc['date']})")
        print(f"   Realized price {fmt(oc['realized'])} | MVRV sekarang {oc['mvrv']:.3f}"
              f" (30h: {oc['mvrv_range'][0]:.2f}-{oc['mvrv_range'][1]:.2f})")
        if oc.get("addr_change") is not None:
            print(f"   Active address 15h terakhir vs sebelumnya: {oc['addr_change'] * 100:+.1f}%")
        if "hashrate_zh" in oc:
            print(f"   Hashrate {oc['hashrate_zh']:.2f} ZH/s")
        print("   Band MVRV:")
        for b, lvl in oc["bands"]:
            mark = "  <- dekat harga" if abs(lvl / p - 1) < 0.02 else ""
            print(f"     MVRV {b:<4} {fmt(lvl):>12}  {pct(lvl, p):>7}{mark}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("coins", nargs="+", help="contoh: BTC LINK ONDO TAO")
    ap.add_argument("--quote", default="USDT")
    ap.add_argument("--price", type=float, help="harga manual (hanya untuk 1 koin)")
    ap.add_argument("--no-onchain", action="store_true")
    args = ap.parse_args()

    ok = 0
    for coin in args.coins:
        coin = coin.upper()
        price = args.price if len(args.coins) == 1 else None
        try:
            a = analyze(coin + args.quote, price)
        except Exception as err:
            print(f"\n[{coin}] gagal ambil data harga: {err}", file=sys.stderr)
            continue
        oc = None
        if not args.no_onchain:
            try:
                oc = onchain(coin.lower(), a["price"])
            except Exception:
                oc = None  # Coin Metrics Community tidak punya semua koin
        report(a, oc)
        ok += 1
    print("\nBukan saran finansial.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
