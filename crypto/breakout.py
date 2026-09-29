#!/usr/bin/env python3
"""Scan token yang belum / baru breakout dari tren bearish (data daily Binance).

Contoh:
    python3 crypto/breakout.py                 # daftar default L1 + RWA
    python3 crypto/breakout.py --group rwa
    python3 crypto/breakout.py SOL SUI ONDO    # koin tertentu

Status:
    BARU BREAKOUT  : sebelumnya bearish (mayoritas di bawah SMA200), sekarang
                     close di atas SMA200 dan cross-nya terjadi <= 30 hari lalu
    BREAKOUT LAMA  : sudah di atas SMA200 lebih dari 30 hari
    MENDEKATI      : masih di bawah SMA200 tapi jaraknya <= 12%, dan sudah
                     di atas SMA50 (tanda awal pembalikan)
    MASIH BEARISH  : di bawah SMA200 dan SMA50
"""

import argparse
import sys

from levels import klines, rsi, sma

GROUPS = {
    "l1": ["ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOT", "NEAR", "SUI", "APT",
           "SEI", "TON", "TRX", "ATOM", "ALGO", "HBAR", "XLM", "ICP", "EGLD", "INJ",
           "S", "TIA", "BERA", "KAIA", "ETC", "LTC", "BCH", "XTZ", "MINA", "CELO",
           "IOTA", "VET", "THETA", "CFX", "FLOW", "KAVA", "ONE", "ROSE", "TAO"],
    "rwa": ["ONDO", "LINK", "PENDLE", "POLYX", "OM", "SYRUP", "PLUME", "TRU", "RSR",
            "USUAL", "CFG", "QNT"],
}


def scan(coin, quote="USDT"):
    c = klines(coin + quote, "1d", 400)
    if len(c) < 230:
        return None  # histori kurang untuk SMA200
    closes = [x["c"] for x in c]
    vols = [x["v"] * x["c"] for x in c]
    price = closes[-1]

    sma200 = [sma(closes[:i + 1], 200) for i in range(len(closes))]
    sma50 = [sma(closes[:i + 1], 50) for i in range(len(closes))]
    above = [s is not None and cl > s for cl, s in zip(closes, sma200)]

    # berapa hari sejak terakhir kali pindah ke atas SMA200
    days_above = 0
    for a in reversed(above):
        if not a:
            break
        days_above += 1

    # fase bearish: porsi hari di bawah SMA200 pada 180 hari sebelum cross / sekarang
    ref_end = len(closes) - days_above
    window = [a for a in above[max(200, ref_end - 180):ref_end] if a is not None]
    bear_share = 1 - sum(window) / len(window) if window else 0

    # golden cross SMA50 > SMA200
    gc_days = None
    for i in range(len(closes) - 1, 200, -1):
        if sma50[i] and sma200[i] and sma50[i] > sma200[i] and sma50[i - 1] <= sma200[i - 1]:
            gc_days = len(closes) - 1 - i
            break
    golden = sma50[-1] > sma200[-1]

    dist200 = price / sma200[-1] - 1
    dist50 = price / sma50[-1] - 1
    high365 = max(x["h"] for x in c[-365:])
    ret30 = price / closes[-31] - 1
    vol_ratio = (sum(vols[-20:]) / 20) / (sum(vols[-90:]) / 90)

    if days_above > 0 and days_above <= 30 and bear_share >= 0.6:
        status = "BARU BREAKOUT"
    elif days_above > 30:
        status = "BREAKOUT LAMA"
    elif days_above > 0:
        status = "BARU BREAKOUT*"  # di atas SMA200 tapi fase sebelumnya tidak terlalu bearish
    elif dist200 >= -0.12 and dist50 > 0:
        status = "MENDEKATI"
    else:
        status = "MASIH BEARISH"

    return {
        "coin": coin, "price": price, "status": status, "days_above": days_above,
        "bear_share": bear_share, "dist200": dist200, "dist50": dist50,
        "golden": golden, "gc_days": gc_days, "dd365": price / high365 - 1,
        "ret30": ret30, "rsi": rsi(closes), "vol_ratio": vol_ratio,
    }


ORDER = ["BARU BREAKOUT", "BARU BREAKOUT*", "MENDEKATI", "BREAKOUT LAMA", "MASIH BEARISH"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("coins", nargs="*")
    ap.add_argument("--group", choices=["l1", "rwa", "all"], default="all")
    args = ap.parse_args()

    if args.coins:
        groups = {"custom": [c.upper() for c in args.coins]}
    elif args.group == "all":
        groups = GROUPS
    else:
        groups = {args.group: GROUPS[args.group]}

    for name, coins in groups.items():
        rows, skipped = [], []
        for coin in coins:
            try:
                r = scan(coin)
            except Exception:
                r = None
            (rows.append(r) if r else skipped.append(coin))
        rows.sort(key=lambda r: (ORDER.index(r["status"]), r["days_above"] or -r["dist200"]))

        print(f"\n=== {name.upper()} ===")
        print(f"{'Koin':<7}{'Status':<16}{'Hari>200':>9}{'Bear%':>7}{'vsSMA200':>10}{'vsSMA50':>9}"
              f"{'GC':>7}{'dari ATH1th':>12}{'30h':>8}{'RSI':>6}{'Vol20/90':>9}")
        for r in rows:
            gc = f"{r['gc_days']}h" if r["golden"] and r["gc_days"] is not None else ("ya" if r["golden"] else "-")
            print(f"{r['coin']:<7}{r['status']:<16}{r['days_above']:>9}{r['bear_share'] * 100:>6.0f}%"
                  f"{r['dist200'] * 100:>+9.1f}%{r['dist50'] * 100:>+8.1f}%{gc:>7}"
                  f"{r['dd365'] * 100:>+11.1f}%{r['ret30'] * 100:>+7.1f}%{r['rsi']:>6.1f}{r['vol_ratio']:>9.2f}")
        if skipped:
            print(f"(tidak ada data / histori < 230 hari: {', '.join(skipped)})")
    print("\nBukan saran finansial.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
