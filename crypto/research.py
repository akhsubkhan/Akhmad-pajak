#!/usr/bin/env python3
"""Riset parameter strategi futures dengan pembagian data latih / uji (walk-forward sederhana).

Contoh:
    python3 crypto/research.py                         # 3 tahun: 2 tahun latih, 1 tahun uji
    python3 crypto/research.py --cache /tmp/hl.pkl     # simpan/pakai ulang data
    python3 crypto/research.py --days 730 --test-days 240

Cara kerja:
    1. Semua kombinasi parameter (lihat grid()) disimulasikan di seluruh periode.
    2. Trade dibagi berdasarkan waktu order dipasang: periode LATIH vs UJI.
    3. Kandidat diurutkan HANYA dari hasil latih (skor = rata-rata R x akar jumlah trade,
       minimal --min-trades trade). Hasil uji tidak dipakai untuk memilih, jadi
       angka uji adalah perkiraan jujur kinerja ke depan.
"""

import argparse
import itertools
import math
import os
import pickle
import sys
import time
from multiprocessing import Pool

import backtest as bt

PRE = None
SPLIT = None


def grid():
    entries = [("market", 0.0, 0)] + [("retest", e, x) for e in (0.0, 0.2, 0.5) for x in (3, 6, 12)]
    exits = [(tp, 0.0) for tp in (1.0, 1.5, 2.0, 3.0)] + [(0.0, tr) for tr in (2.0, 3.0, 4.0)]
    for (mode, e, x), sl, (tp, tr), side, trend, score, mp in itertools.product(
            entries, (0.8, 1.2, 1.8, 2.5), exits, ("both", "long", "short"), (False, True), (2, 3), (2, 4)):
        yield bt.Params(tp_r=tp, sl_atr=sl, entry_atr=e, market=mode == "market", trail=tr, expiry=x,
                        side=side, trend_only=trend, min_score=score, max_pos=mp)


def period_stats(trades):
    n = len(trades)
    if not n:
        return {"n": 0, "wr": 0.0, "avg": 0.0, "tot": 0.0, "pf": 0.0, "dd": 0.0}
    rs = [t["r"] for t in trades]
    gw = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r <= 0)
    cum = peak = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"n": n, "wr": sum(1 for r in rs if r > 0) / n, "avg": sum(rs) / n, "tot": sum(rs),
            "pf": gw / gl if gl else 99.0, "dd": dd}


def work(p):
    res = bt.simulate(PRE, p, t_start=SPLIT[0])
    tr = sorted(res["trades"], key=lambda t: t["closed"])
    train = [t for t in tr if t["placed"] < SPLIT[1]]
    test = [t for t in tr if t["placed"] >= SPLIT[1]]
    return p, period_stats(train), period_stats(test)


def label(p):
    entry = "market" if p.market else f"retest{p.entry_atr:g}/exp{p.expiry}"
    exit_ = f"TP{p.tp_r:g}R" if p.tp_r else f"trail{p.trail:g}"
    return (f"{entry:<14} SL{p.sl_atr:<4g}{exit_:<8}{p.side:<6}{'tren' if p.trend_only else '-':<5}"
            f"s>={p.min_score} pos{p.max_pos}")


def fmt(s):
    return (f"{s['n']:>4} {s['wr'] * 100:>4.0f}% {s['avg']:>+6.2f} {s['tot']:>+6.1f} "
            f"{min(s['pf'], 9.99):>5.2f} {s['dd']:>5.1f}")


def main():
    global PRE, SPLIT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=1095, help="total periode (hari)")
    ap.add_argument("--test-days", type=int, default=365, help="periode uji di akhir (hari)")
    ap.add_argument("--top", type=int, default=80)
    ap.add_argument("--min-vol", type=float, default=20)
    ap.add_argument("--coins", nargs="*")
    ap.add_argument("--min-trades", type=int, default=60, help="minimal trade di periode latih")
    ap.add_argument("--show", type=int, default=25)
    ap.add_argument("--cache", help="file pickle untuk menyimpan/memakai ulang data candle")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    args = ap.parse_args()

    if args.cache and os.path.exists(args.cache):
        with open(args.cache, "rb") as f:
            data = pickle.load(f)
    else:
        coins = bt.universe(args)
        print(f"Mengambil data {len(coins)} koin ({args.days} hari)...", file=sys.stderr)
        data = bt.load_all(coins, args.days)
        if args.cache:
            with open(args.cache, "wb") as f:
                pickle.dump(data, f)
    now = int(time.time() * 1000)
    SPLIT = (now - args.days * bt.D1_MS, now - args.test_days * bt.D1_MS)
    t0 = time.time()
    PRE = bt.precompute(data)
    configs = list(grid())
    print(f"{len(data)} koin, sinyal dihitung ({time.time() - t0:.0f} dtk). "
          f"Simulasi {len(configs)} kombinasi...", file=sys.stderr)
    with Pool(args.workers) as pool:
        results = pool.map(work, configs, chunksize=20)

    ok = [r for r in results if r[1]["n"] >= args.min_trades]
    ok.sort(key=lambda r: -r[1]["avg"] * math.sqrt(r[1]["n"]))
    hdr = f"{'n':>4} {'WR':>5} {'avgR':>6} {'totR':>6} {'PF':>5} {'DD_R':>5}"
    print(f"\nLatih: {args.days - args.test_days} hari | Uji: {args.test_days} hari terakhir | "
          f"{len(ok)}/{len(results)} kombinasi dengan >= {args.min_trades} trade latih")
    print(f"\n{'Parameter':<48} | LATIH {hdr} | UJI {hdr}")
    for p, a, b in ok[:args.show]:
        print(f"{label(p):<48} |       {fmt(a)} |     {fmt(b)}")

    def summary(rows, name):
        if not rows:
            return
        avgs = sorted(r[2]["avg"] for r in rows if r[2]["n"])
        pos = sum(1 for a in avgs if a > 0)
        print(f"  {name:<34}: median avgR uji {avgs[len(avgs) // 2]:+.3f}, "
              f"uji positif {pos}/{len(avgs)}")

    print("\nRingkasan hasil UJI:")
    summary(ok[:10], "10 terbaik (dipilih dari latih)")
    summary(ok[:50], "50 terbaik (dipilih dari latih)")
    summary(ok, "semua kombinasi")
    for p, a, b in results:
        if (p.tp_r, p.sl_atr, p.entry_atr, p.market, p.trail, p.expiry, p.side, p.trend_only,
                p.min_score, p.max_pos) == (1.5, 1.2, 0.2, False, 0.0, 12, "both", False, 2, 2):
            print(f"\nSetelan bot sekarang (+expiry 12):\n{label(p):<48} |       {fmt(a)} |     {fmt(b)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
