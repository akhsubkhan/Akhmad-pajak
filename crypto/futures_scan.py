#!/usr/bin/env python3
"""Scan koin likuid yang baru breakout / breakdown, plus rencana trade futures.

Contoh:
    python3 crypto/futures_scan.py                    # top 80 pair USDT by volume
    python3 crypto/futures_scan.py --top 120 --min-vol 30
    python3 crypto/futures_scan.py --capital 3634 --risk 1

Deteksi (hanya candle yang sudah close):
    Breakout 4H  : close > high tertinggi 42 candle 4H sebelumnya (7 hari)
                   dan volume > 1,5x rata-rata 20 candle
    Breakout 1D  : close harian > high tertinggi 20 hari sebelumnya
    Breakdown    : kebalikannya (kandidat short)

Rencana trade (ATR 4H):
    Entry  = retest level breakout (+0,2 ATR)
    SL     = level - 1,2 ATR
    TP1/2  = 1,5R / 3R
    Size   = (modal x risk%) / jarak SL
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

from levels import atr, binance, ema, klines, rsi

STABLE = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "AEUR", "USDE",
          "XUSD", "USD1", "BFUSD", "RLUSD", "PAXG", "XAUT", "WBTC", "WBETH"}


def liquid_pairs(top, min_vol_m):
    rows = binance("/api/v3/ticker/24hr", {})
    out = []
    for r in rows:
        sym = r["symbol"]
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in STABLE or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue
        qv = float(r["quoteVolume"])
        if qv >= min_vol_m * 1e6:
            out.append((base, qv, float(r["priceChangePercent"])))
    out.sort(key=lambda x: -x[1])
    return out[:top]


def analyze(base):
    sym = base + "USDT"
    h4 = klines(sym, "4h", 260)
    d1 = klines(sym, "1d", 60)
    if len(h4) < 220 or len(d1) < 25:
        return None
    price = h4[-1]["c"]
    h4c, d1c = h4[:-1], d1[:-1]  # hanya candle yang sudah close
    a = atr(h4c)
    closes4 = [c["c"] for c in h4c]
    ema50, ema200 = ema(closes4, 50), ema(closes4, 200)
    vol_avg = sum(c["v"] for c in h4c[-21:-1]) / 20
    last4 = h4c[-1]

    signals = []
    # breakout / breakdown 4H: cek 3 candle terakhir
    for k in range(1, 4):
        c = h4c[-k]
        prev = h4c[-k - 42:-k]
        hi, lo = max(x["h"] for x in prev), min(x["l"] for x in prev)
        vol_ok = c["v"] > 1.5 * vol_avg
        if c["c"] > hi and vol_ok:
            signals.append(("LONG", "4H", hi, k))
            break
        if c["c"] < lo and vol_ok:
            signals.append(("SHORT", "4H", lo, k))
            break
    # breakout / breakdown 1D: cek 2 candle harian terakhir
    for k in range(1, 3):
        c = d1c[-k]
        prev = d1c[-k - 20:-k]
        hi, lo = max(x["h"] for x in prev), min(x["l"] for x in prev)
        if c["c"] > hi:
            signals.append(("LONG", "1D", hi, k))
            break
        if c["c"] < lo:
            signals.append(("SHORT", "1D", lo, k))
            break
    if not signals:
        return None

    # pakai sinyal 1D kalau ada (lebih kuat), else 4H
    side, tf, level, ago = sorted(signals, key=lambda s: s[1] != "1D")[0]
    both = len({s[1] for s in signals if s[0] == side}) == 2
    trend_ok = (price > ema50 > ema200) if side == "LONG" else (price < ema50 < ema200)
    ext = (price - level) / a if side == "LONG" else (level - price) / a

    if side == "LONG":
        entry = level + 0.2 * a
        sl = level - 1.2 * a
        r = entry - sl
        tp1, tp2 = entry + 1.5 * r, entry + 3 * r
    else:
        entry = level - 0.2 * a
        sl = level + 1.2 * a
        r = sl - entry
        tp1, tp2 = entry - 1.5 * r, entry - 3 * r

    failed = ext < -0.3  # harga sudah kembali ke sisi lain level
    score = (2 if tf == "1D" else 1) + (1 if both else 0) + (1 if trend_ok else 0) \
        - (1 if ext > 2 else 0) - (1 if ext > 4 else 0) - (3 if failed else 0)
    return {
        "coin": base, "side": side, "tf": "4H+1D" if both else tf, "ago": ago,
        "price": price, "level": level, "ext": ext, "trend_ok": trend_ok,
        "rsi4": rsi(closes4), "vol_x": last4["v"] / vol_avg if vol_avg else 0,
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
        "sl_pct": r / entry, "score": score, "failed": failed,
    }


def fmt(x):
    if x >= 100:
        return f"{x:,.1f}"
    if x >= 1:
        return f"{x:.3f}"
    return f"{x:.5f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=80, help="jumlah pair teratas by volume")
    ap.add_argument("--min-vol", type=float, default=20, help="min volume 24 jam (juta USDT)")
    ap.add_argument("--capital", type=float, default=0, help="modal (USDT) untuk hitung size")
    ap.add_argument("--risk", type=float, default=1.0, help="risk per trade (%% modal)")
    args = ap.parse_args()

    pairs = liquid_pairs(args.top, args.min_vol)
    vol24 = {b: v for b, v, _ in pairs}
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda p: _safe(analyze, p[0]), pairs))
    rows = sorted([r for r in results if r], key=lambda r: (-r["score"], r["ext"]))

    print(f"Discan {len(pairs)} pair (vol 24j >= ${args.min_vol:.0f}M). Sinyal: {len(rows)}\n")
    print(f"{'Koin':<8}{'Arah':<6}{'TF':<6}{'Skor':>4}{'Harga':>11}{'Level':>11}{'Jauh(ATR)':>10}"
          f"{'Tren':>6}{'RSI4H':>6}{'Vol24j':>8}")
    for r in rows:
        print(f"{r['coin']:<8}{r['side']:<6}{r['tf']:<6}{r['score']:>4}{fmt(r['price']):>11}"
              f"{fmt(r['level']):>11}{r['ext']:>10.1f}{'ya' if r['trend_ok'] else '-':>6}"
              f"{r['rsi4']:>6.0f}{vol24[r['coin']] / 1e6:>7.0f}M")

    print("\nRENCANA TRADE (skor >= 2, belum jauh dari level, SL <= 12%)")
    for r in rows:
        if r["score"] < 2 or r["ext"] > 4 or r["failed"] or r["sl_pct"] > 0.12:
            continue
        line = (f"  {r['coin']:<7}{r['side']:<6} entry {fmt(r['entry'])}  SL {fmt(r['sl'])} "
                f"({r['sl_pct'] * 100:.1f}%)  TP1 {fmt(r['tp1'])}  TP2 {fmt(r['tp2'])}")
        if args.capital:
            risk_usd = args.capital * args.risk / 100
            pos = risk_usd / r["sl_pct"]
            line += f"  | posisi ~{pos:,.0f} USDT (risk {risk_usd:.0f})"
        print(line)
    print("\nFutures berisiko tinggi. Bukan saran finansial.")
    return 0


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(main())
