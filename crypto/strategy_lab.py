#!/usr/bin/env python3
"""Backtest dua strategi 4H: Volume Spike + Momentum, dan Funding Rate Contrarian.

Contoh:
    python3 crypto/strategy_lab.py volume  --cache /tmp/hl3y.pkl
    python3 crypto/strategy_lab.py funding --cache /tmp/hl3y.pkl --funding /tmp/funding.pkl

Data: candle 4H/1D Binance (pickle dari research.py --cache). Funding: riwayat funding per jam
Hyperliquid mainnet, pickle {koin: [(waktu_ms, rate_per_jam), ...]}.

VOLUME SPIKE + MOMENTUM (dievaluasi tiap candle 4H close):
    - Volume candle terakhir >= M x rata-rata 20 candle sebelumnya, dan
    - badan candle (close - open) >= B x ATR searah (hijau -> LONG, merah -> SHORT),
    - opsional: searah tren (close di atas/bawah EMA200 4H).
    Masuk market di open candle berikutnya.
FUNDING RATE CONTRARIAN:
    - Rata-rata funding 24 jam terakhir (disetahunkan) >= batas atas -> SHORT (long terlalu ramai),
      <= batas bawah -> LONG (short terlalu ramai).
    - Opsional konfirmasi: candle 4H terakhir sudah berbalik arah (merah untuk SHORT, hijau untuk LONG).
Keduanya: SL = sl_atr x ATR dari entry, TP = tp_r x jarak SL, risk 1%/trade, maks 4 posisi,
leverage 3x; eksekusi & fee sama dengan backtest.py. Funding yang dibayar/diterima selama posisi
terbuka dihitung terpisah (kolom "R+funding") untuk koin yang punya data funding.
Pemilihan setelan memakai periode LATIH (2 tahun pertama); periode UJI (1 tahun terakhir) tidak
dipakai untuk memilih.
"""

import argparse
import bisect
import itertools
import math
import pickle
import sys
from multiprocessing import Pool

import backtest as bt
from ml_experiment import ema_series

H4, D1, Y = bt.H4_MS, bt.D1_MS, 365 * bt.D1_MS
G = {}  # data bersama untuk worker


def series(h4):
    cl = [c["c"] for c in h4]
    tr = [0.0] + [max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])) for p, c in zip(h4, h4[1:])]
    return cl, tr, ema_series(cl, 200)


def sig(b, side, a, px, prio):
    return {"coin": b, "side": side, "tf": "X", "score": 3, "ext": prio, "trend_ok": True,
            "atr": a, "level": px, "price": px, "failed": False}


def volume_signals(data, m_vol, body, trend):
    out = {}
    for b, h4, _ in data:
        cl, tr, e200 = series(h4)
        for k in range(220, len(h4)):
            j = k - 1
            c = h4[j]
            avg = sum(x["v"] for x in h4[j - 20:j]) / 20
            a = sum(tr[k - 14:k]) / 14
            if not avg or not a or c["v"] < m_vol * avg:
                continue
            mv = (c["c"] - c["o"]) / a
            side = "LONG" if mv >= body else "SHORT" if mv <= -body else None
            if side is None:
                continue
            if trend and ((side == "LONG") != (cl[j] > e200[j])):
                continue
            out.setdefault(h4[k]["t"], []).append(sig(b, side, a, h4[k]["o"], -c["v"] / avg))
    return out


def funding_24h(fund, t):
    """Rata-rata funding per jam selama 24 jam sebelum t, disetahunkan (%)."""
    ts, rs = fund
    i = bisect.bisect_left(ts, t)
    if i < 20:
        return None
    win = rs[max(0, i - 24):i]
    return sum(win) / len(win) * 24 * 365 * 100


def funding_signals(data, funding, hi, lo, confirm):
    out = {}
    for b, h4, _ in data:
        if b not in funding:
            continue
        cl, tr, _ = series(h4)
        for k in range(220, len(h4)):
            f = funding_24h(funding[b], h4[k]["t"])
            if f is None:
                continue
            side = "SHORT" if f >= hi else "LONG" if f <= lo else None
            if side is None:
                continue
            c = h4[k - 1]
            if confirm and ((side == "SHORT") != (c["c"] < c["o"])):
                continue
            a = sum(tr[k - 14:k]) / 14
            if a:
                out.setdefault(h4[k]["t"], []).append(sig(b, side, a, h4[k]["o"], -abs(f)))
    return out


def funding_r(tr, funding):
    """Funding yang dibayar (+) / diterima (-) selama posisi, dalam R. Long membayar funding positif."""
    if tr["coin"] not in funding:
        return 0.0
    ts, rs = funding[tr["coin"]]
    i, j = bisect.bisect_left(ts, tr["filled"]), bisect.bisect_left(ts, tr["closed"])
    paid = sum(rs[i:j]) * (1 if tr["side"] == "LONG" else -1)
    return -paid * tr["entry"] / abs(tr["entry"] - tr["sl"])


def work(job):
    key, p = job
    pre = (G["h4s"], G["idx"], G["sigs"][key])
    res = bt.simulate(pre, p, t_start=G["t0"])
    out = []
    for lo_, hi_ in ((G["t0"], G["split"]), (G["split"], G["end"])):
        tr = [t for t in res["trades"] if lo_ <= t["placed"] < hi_]
        rs = [t["r"] for t in tr]
        rf = [t["r"] + funding_r(t, G["funding"]) for t in tr]
        n = len(rs)
        out.append({"n": n, "wr": sum(r > 0 for r in rs) / n if n else 0, "avg": sum(rs) / n if n else 0,
                    "avgf": sum(rf) / n if n else 0})
    s = bt.stats(res)
    return key, p, out[0], out[1], s["ret"], s["dd"]


def run(jobs, label_fn, title):
    with Pool() as pool:
        res = pool.map(work, jobs)
    ok = [r for r in res if r[2]["n"] >= 60]
    ok.sort(key=lambda r: -r[2]["avg"] * math.sqrt(r[2]["n"]))
    f = lambda s: f"{s['n']:>4} {s['wr'] * 100:>3.0f}% {s['avg']:>+6.2f}R {s['avgf']:>+6.2f}R"
    print(f"\n{title}: {len(res)} kombinasi, {len(ok)} dengan >= 60 trade latih")
    print(f"{'Setelan':<44} | LATIH    n   WR   rata2 R+fund | UJI    n   WR   rata2 R+fund | 3 thn hasil/DD")
    for key, p, a, b, ret, dd in ok[:12]:
        print(f"{label_fn(key, p):<44} |       {f(a)} |     {f(b)} | {ret * 100:+5.0f}% / {dd * 100:.0f}%")
    print(f"Untung di latih: {sum(r[2]['avg'] > 0 for r in ok)}/{len(ok)} | untung di uji: "
          f"{sum(r[3]['avg'] > 0 for r in ok)}/{len(ok)} | 10 teratas (dipilih dr latih) untung di uji: "
          f"{sum(r[3]['avg'] > 0 for r in ok[:10])}/10")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("strategy", choices=["volume", "funding"])
    ap.add_argument("--cache", required=True)
    ap.add_argument("--funding", help="pickle riwayat funding Hyperliquid")
    args = ap.parse_args()

    data = pickle.load(open(args.cache, "rb"))
    funding = {}
    if args.funding:
        raw = pickle.load(open(args.funding, "rb"))
        funding = {c: ([t for t, _ in v], [r for _, r in v]) for c, v in raw.items() if v}
    end = max(h4[-1]["t"] for _, h4, _ in data) + H4
    G.update(h4s={b: h4 for b, h4, _ in data}, idx={b: {c["t"]: i for i, c in enumerate(h4)} for b, h4, _ in data},
             t0=end - 3 * Y, split=end - Y, end=end, funding=funding, sigs={})
    sides = ("long", "short", "both")
    exits = list(itertools.product((1.0, 1.5, 2.0), (1.0, 2.0, 3.0)))

    if args.strategy == "volume":
        keys = list(itertools.product((2.0, 3.0, 4.0), (0.5, 1.0, 1.5), (False, True)))
        for k in keys:
            G["sigs"][k] = volume_signals(data, *k)
        jobs = [(k, bt.Params(market=True, sl_atr=sl, tp_r=tp, side=sd, min_score=3, max_pos=4, max_ext=0))
                for k in keys for sl, tp in exits for sd in sides]
        lab = lambda k, p: (f"vol>={k[0]:g}x badan>={k[1]:g}ATR {'tren' if k[2] else '-':<4} "
                            f"SL{p.sl_atr:g} TP{p.tp_r:g}R {p.side}")
        run(jobs, lab, "VOLUME SPIKE + MOMENTUM")
    else:
        if not funding:
            sys.exit("--funding wajib untuk strategi funding")
        keys = list(itertools.product((20.0, 40.0, 80.0), (-5.0, -20.0, -40.0), (False, True)))
        for k in keys:
            G["sigs"][k] = funding_signals(data, funding, *k)
        jobs = [(k, bt.Params(market=True, sl_atr=sl, tp_r=tp, side=sd, min_score=3, max_pos=4, max_ext=0))
                for k in keys for sl, tp in exits for sd in sides]
        lab = lambda k, p: (f"fund>={k[0]:g}%/<={k[1]:g}% {'konf' if k[2] else '-':<4} "
                            f"SL{p.sl_atr:g} TP{p.tp_r:g}R {p.side}")
        run(jobs, lab, "FUNDING RATE CONTRARIAN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
